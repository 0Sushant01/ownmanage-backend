from typing import Dict, Any, Optional
from datetime import time
from apps.attendance.models import AttendancePolicy, AttendancePolicyOverride
from apps.organization.models import Branch, Business
from apps.organization.services.permission_service import PermissionService

# Global fallback defaults if neither Enterprise nor Center has policy defined
SYSTEM_DEFAULTS = {
    'office_start': '09:00',
    'office_end': '18:00',
    'break_start': '13:00',
    'break_end': '14:00',
    'working_days': 5,
    'weekly_off': 6,  # legacy Sunday
    'weekly_off_days': [6],  # 0=Monday, 6=Sunday
    'daily_schedules': {},
    'grace_period_minutes': 15,
    'minimum_present_minutes': 480,
    'minimum_half_day_minutes': 240,
    'late_threshold_minutes': 30,
    'early_checkout_threshold_minutes': 30,
    'auto_attendance': True,
    'ot_enabled': False,
    'ot_grace_minutes': 30,
    'ot_approval_required': True,
    'max_daily_ot_minutes': 240,
    'ot_rate_multiplier': 1.0,
    'allow_normal_punch': True,
    'allow_gps': True,
    'allow_geofencing': False,
    'allow_qr': False,
    'allow_face_recognition': False,
    'allow_biometric': False,
    'gps_latitude': None,
    'gps_longitude': None,
    'gps_radius_meters': 100,
    'location_required_checkin': False,
    'location_required_checkout': False,
}


class PolicyResolver:
    ALL_RESOLVABLE_FIELDS = [
        'office_start',
        'office_end',
        'break_start',
        'break_end',
        'working_days',
        'weekly_off',
        'weekly_off_days',
        'daily_schedules',
        'grace_period_minutes',
        'minimum_present_minutes',
        'minimum_half_day_minutes',
        'late_threshold_minutes',
        'early_checkout_threshold_minutes',
        'auto_attendance',
        'ot_enabled',
        'ot_grace_minutes',
        'ot_approval_required',
        'max_daily_ot_minutes',
        'ot_rate_multiplier',
        'allow_normal_punch',
        'allow_gps',
        'allow_geofencing',
        'allow_qr',
        'allow_face_recognition',
        'allow_biometric',
        'gps_latitude',
        'gps_longitude',
        'gps_radius_meters',
        'location_required_checkin',
        'location_required_checkout',
    ]

    @classmethod
    def get_attendance_policy(cls, centre: Optional[Branch] = None, business: Optional[Business] = None, user=None) -> Dict[str, Any]:
        """
        Resolves effective attendance policy using the hierarchy:
        Center Override -> Enterprise Default -> System Default.
        Returns resolved effective values, source provenance, and editing permissions.
        """
        if centre and not business:
            business = centre.business

        # 1. Load Enterprise default policy
        ent_policy = None
        if business:
            ent_policy = AttendancePolicy.objects.filter(business=business).first()

        # 2. Load Centre override if centre provided
        centre_override = None
        if centre:
            centre_override = AttendancePolicyOverride.objects.filter(centre=centre).first()

        effective = {}
        source = {}
        enterprise_defaults = {}
        centre_overrides_dict = {}

        for field in cls.ALL_RESOLVABLE_FIELDS:
            # Value from Enterprise Policy
            ent_val = getattr(ent_policy, field, None) if ent_policy else None
            is_ent_defined = (ent_val is not None)

            if not is_ent_defined:
                # If latitude/longitude is not on ent_policy, check if centre has it or use system default
                ent_val = SYSTEM_DEFAULTS.get(field)

            if isinstance(ent_val, time):
                ent_val = ent_val.strftime('%H:%M')

            enterprise_defaults[field] = ent_val

            # Value from Centre Override
            ovr_val = getattr(centre_override, field, None) if centre_override else None
            # Treat None as not overridden
            if ovr_val is not None:
                if isinstance(ovr_val, time):
                    ovr_val = ovr_val.strftime('%H:%M')
                centre_overrides_dict[field] = ovr_val
                effective[field] = ovr_val
                source[field] = 'center'
            else:
                effective[field] = ent_val
                source[field] = 'enterprise' if is_ent_defined else 'system'

        # Merge extra settings for attendance-to-payroll policies (Enterprise -> Centre override)
        ent_extra = ent_policy.extra_settings if (ent_policy and ent_policy.extra_settings) else {}
        ovr_extra = centre_override.extra_settings if (centre_override and centre_override.extra_settings) else {}
        effective_extra = {
            'late_deduction_enabled': True,
            'late_deduction_fraction': 0.5,
            'half_day_deduction_fraction': 0.5,
            'holiday_work_multiplier': 1.0,
            'weekly_off_work_multiplier': 1.0,
            'absence_deduction_divisor': 30,
            **ent_extra,
            **ovr_extra
        }
        for k, v in effective_extra.items():
            if k not in effective:
                effective[k] = v

        # Special handling for GPS coordinates fallback to Branch record if unconfigured
        if centre:
            if effective.get('gps_latitude') is None and centre.latitude is not None:
                effective['gps_latitude'] = float(centre.latitude)
                source['gps_latitude'] = 'center_branch_record'
            if effective.get('gps_longitude') is None and centre.longitude is not None:
                effective['gps_longitude'] = float(centre.longitude)
                source['gps_longitude'] = 'center_branch_record'
            if effective.get('gps_radius_meters') in [None, 100] and centre.geofence_radius:
                effective['gps_radius_meters'] = centre.geofence_radius

        # Determine editing permissions
        can_edit = False
        allow_center_override = ent_policy.allow_center_override if ent_policy else True

        if user:
            if user.is_superuser:
                can_edit = True
            elif centre:
                can_edit = allow_center_override and PermissionService.has_permission(
                    user=user,
                    permission_key='attendance.manage_policy',
                    business=business,
                    centre=centre
                )
            else:
                membership = user.business_memberships.filter(business=business, is_active=True).first() if business else None
                can_edit = bool(membership and membership.role in ['BUSINESS_ADMIN', 'SUPERADMIN'])

        return {
            'effective': effective,
            'source': source,
            'enterprise_default': enterprise_defaults,
            'center_override': centre_overrides_dict,
            'has_override': bool(centre_override is not None and len(centre_overrides_dict) > 0),
            'allow_center_override': allow_center_override,
            'can_edit': can_edit,
            'centre_id': str(centre.id) if centre else None,
            'centre_name': centre.name if centre else None,
            'business_id': str(business.id) if business else None,
        }

    @classmethod
    def save_centre_override(cls, centre: Branch, override_data: Dict[str, Any], user) -> AttendancePolicyOverride:
        """
        Saves or updates a centre-level attendance override with individual field reset support.
        """
        override, _ = AttendancePolicyOverride.objects.get_or_create(centre=centre)

        normalized_data = dict(override_data)
        if 'office_start_time' in normalized_data and 'office_start' not in normalized_data:
            normalized_data['office_start'] = normalized_data['office_start_time']
        if 'office_end_time' in normalized_data and 'office_end' not in normalized_data:
            normalized_data['office_end'] = normalized_data['office_end_time']

        # Handle specific field resets
        reset_fields = normalized_data.get('reset_fields', [])
        for rf in reset_fields:
            if hasattr(override, rf):
                setattr(override, rf, None)

        for field in cls.ALL_RESOLVABLE_FIELDS:
            if field in normalized_data and field not in reset_fields:
                val = normalized_data[field]
                if val == '__RESET__' or val == 'RESET':
                    val = None
                setattr(override, field, val)

        override.save()
        return override

    @staticmethod
    def reset_centre_override(centre: Branch, user, field_name: Optional[str] = None) -> bool:
        """
        Removes the centre override or resets a specific field back to Enterprise default.
        """
        if field_name:
            override = AttendancePolicyOverride.objects.filter(centre=centre).first()
            if override and hasattr(override, field_name):
                setattr(override, field_name, None)
                override.save()
                return True
            return False

        deleted_count, _ = AttendancePolicyOverride.objects.filter(centre=centre).delete()
        return deleted_count > 0


def resolve_effective_policy(centre: Optional[Branch] = None, business: Optional[Business] = None, user=None) -> Dict[str, Any]:
    """Helper alias function to resolve effective policy."""
    return PolicyResolver.get_attendance_policy(centre=centre, business=business, user=user)
