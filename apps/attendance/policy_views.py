from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, NotFound, ValidationError

from apps.core.permissions import get_user_context
from apps.organization.models import Branch, Business, BusinessRole
from apps.attendance.models import AttendancePolicy, AttendancePolicyOverride
from apps.attendance.serializers import AttendancePolicySerializer, AttendancePolicyOverrideSerializer
from apps.organization.services.policy_resolver import PolicyResolver
from apps.organization.services.permission_service import PermissionService
from apps.core.services.audit_service import AuditService


class EnterpriseAttendancePolicyView(views.APIView):
    """
    Enterprise Admin views and manages global attendance policy defaults.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if not biz:
            raise PermissionDenied('No active enterprise context.')

        policy_data = PolicyResolver.get_attendance_policy(business=biz, user=request.user)
        return Response(policy_data)

    def put(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if not (ctx['is_superadmin'] or ctx['role'] == BusinessRole.BUSINESS_ADMIN):
            raise PermissionDenied('Only Enterprise Admin can configure enterprise attendance policy.')

        policy, _ = AttendancePolicy.objects.get_or_create(business=biz)
        old_data = AttendancePolicySerializer(policy).data

        serializer = AttendancePolicySerializer(policy, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        policy = serializer.save()

        AuditService.log(
            user_or_request=request,
            action='UPDATE_ENTERPRISE_ATTENDANCE_POLICY',
            entity_type='AttendancePolicy',
            entity_id=str(policy.id),
            old_data=old_data,
            new_data=serializer.data,
            business=biz,
            reason='Enterprise Admin updated global attendance defaults'
        )

        return Response(PolicyResolver.get_attendance_policy(business=biz, user=request.user))


class CentreAttendancePolicyView(views.APIView):
    """
    Returns resolved effective policy for a Center, and allows permitted Managers / Enterprise Admins
    to save Center-specific overrides.
    """
    permission_classes = [IsAuthenticated]

    def get_centre(self, request, pk):
        ctx = get_user_context(request)
        centre = Branch.objects.filter(id=pk).select_related('business').first()
        if not centre:
            raise NotFound('Centre not found.')

        if not ctx['is_superadmin'] and centre.business_id != ctx['business'].id:
            raise PermissionDenied('Cross-tenant centre access forbidden.')

        return centre

    def get(self, request, pk):
        centre = self.get_centre(request, pk)
        policy_data = PolicyResolver.get_attendance_policy(centre=centre, user=request.user)
        return Response(policy_data)

    def put(self, request, pk):
        centre = self.get_centre(request, pk)
        biz = centre.business

        # Verify editing permission: Enterprise Admin or Manager with attendance.manage_policy
        can_edit = False
        if request.user.is_superuser:
            can_edit = True
        else:
            membership = request.user.business_memberships.filter(business=biz, is_active=True).first()
            if membership and membership.role == BusinessRole.BUSINESS_ADMIN:
                can_edit = True
            elif membership and membership.role == BusinessRole.MANAGER:
                ent_policy = AttendancePolicy.objects.filter(business=biz).first()
                if ent_policy and not ent_policy.allow_center_override:
                    raise PermissionDenied('Enterprise Administrator has disabled Center-level policy overrides.')
                can_edit = PermissionService.has_permission(
                    user=request.user,
                    permission_key='attendance.manage_policy',
                    business=biz,
                    centre=centre
                )

        if not can_edit:
            raise PermissionDenied('You do not have permission to modify Centre attendance policy overrides.')

        # Capture old data
        old_override = AttendancePolicyOverride.objects.filter(centre=centre).first()
        old_data = AttendancePolicyOverrideSerializer(old_override).data if old_override else None

        override = PolicyResolver.save_centre_override(centre=centre, override_data=request.data, user=request.user)

        AuditService.log(
            user_or_request=request,
            action='UPDATE_CENTRE_ATTENDANCE_OVERRIDE',
            entity_type='AttendancePolicyOverride',
            entity_id=str(override.id),
            old_data=old_data,
            new_data=AttendancePolicyOverrideSerializer(override).data,
            business=biz,
            reason=f'Updated attendance override for centre {centre.name}'
        )

        return Response(PolicyResolver.get_attendance_policy(centre=centre, user=request.user))


class CentreAttendancePolicyResetView(views.APIView):
    """
    Resets Center override (all fields or a specific field), reverting to Enterprise Defaults.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        centre = Branch.objects.filter(id=pk).select_related('business').first()
        if not centre:
            raise NotFound('Centre not found.')

        ctx = get_user_context(request)
        if not ctx['is_superadmin'] and centre.business_id != ctx['business'].id:
            raise PermissionDenied('Cross-tenant operation forbidden.')

        can_reset = PermissionService.has_permission(
            user=request.user,
            permission_key='attendance.manage_policy',
            business=centre.business,
            centre=centre
        )
        if not can_reset:
            raise PermissionDenied('You do not have permission to reset Centre attendance policy.')

        field_name = request.data.get('field') or request.data.get('field_name')
        PolicyResolver.reset_centre_override(centre=centre, user=request.user, field_name=field_name)

        AuditService.log(
            user_or_request=request,
            action='RESET_CENTRE_ATTENDANCE_OVERRIDE',
            entity_type='AttendancePolicyOverride',
            entity_id=str(centre.id),
            business=centre.business,
            reason=f"Reset {field_name or 'all overrides'} to Enterprise default for centre {centre.name}"
        )

        policy_data = PolicyResolver.get_attendance_policy(centre=centre, user=request.user)
        msg = f"Reset {field_name} to Enterprise default successfully." if field_name else f"Centre '{centre.name}' reset to Enterprise defaults successfully."
        return Response({
            'detail': msg,
            **policy_data
        })


class EmployeeWorkingHoursView(views.APIView):
    """
    Returns effective working hours and schedule configuration for an employee,
    with explicit configuration provenance (Enterprise Default vs Centre Override vs Employee Specific).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from apps.organization.models import Employee
        emp = Employee.objects.filter(id=pk).select_related('business', 'branch').first()
        if not emp:
            raise NotFound('Employee not found.')

        ctx = get_user_context(request)
        if not ctx['is_superadmin'] and emp.business_id != ctx['business'].id:
            raise PermissionDenied('Cross-tenant access forbidden.')

        centre = emp.branch
        biz = emp.business
        policy_data = PolicyResolver.get_attendance_policy(centre=centre, business=biz, user=request.user)

        # Check if employee has a specific schedule assignment
        from apps.attendance.models import EmployeeScheduleAssignment
        assignment = EmployeeScheduleAssignment.objects.filter(
            employee=emp,
            effective_to__isnull=True
        ).select_related('schedule').order_by('-effective_from').first()

        effective = policy_data.get('effective', {})
        source_dict = policy_data.get('source', {})

        # Determine overall configuration source
        has_centre_override = centre and any(src == 'center' for src in source_dict.values())
        if assignment:
            config_source = 'Employee Custom Schedule'
            source_badge = 'Employee Specific'
        elif has_centre_override:
            config_source = f"Centre Policy ({centre.name})"
            source_badge = 'Inherited from Centre'
        elif centre:
            config_source = f"Enterprise Default (inherited by {centre.name})"
            source_badge = 'Inherited from Enterprise'
        else:
            config_source = "Enterprise Default Policy"
            source_badge = 'Inherited from Enterprise'

        # Map day numbers to names
        day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        weekly_off_days_indices = effective.get('weekly_off_days') or [6]
        weekly_off_names = [day_names[i] for i in weekly_off_days_indices if 0 <= i < 7]

        return Response({
            'employee_id': str(emp.id),
            'employee_name': emp.full_name,
            'centre_id': str(centre.id) if centre else None,
            'centre_name': centre.name if centre else 'Unassigned Centre',
            'configuration_source': config_source,
            'source_badge': source_badge,
            'shift_timings': {
                'office_start': effective.get('office_start', '09:00'),
                'office_end': effective.get('office_end', '18:00'),
                'break_start': effective.get('break_start', '13:00'),
                'break_end': effective.get('break_end', '14:00'),
            },
            'rules': {
                'grace_period_minutes': effective.get('grace_period_minutes', 15),
                'minimum_present_minutes': effective.get('minimum_present_minutes', 480),
                'minimum_half_day_minutes': effective.get('minimum_half_day_minutes', 240),
                'late_threshold_minutes': effective.get('late_threshold_minutes', 30),
                'early_checkout_threshold_minutes': effective.get('early_checkout_threshold_minutes', 30),
                'working_days_per_week': effective.get('working_days', 5),
                'weekly_off_days': weekly_off_names,
                'ot_enabled': effective.get('ot_enabled', False),
                'ot_grace_minutes': effective.get('ot_grace_minutes', 30),
                'max_daily_ot_minutes': effective.get('max_daily_ot_minutes', 240),
            },
            'verification_methods': {
                'normal_punch': effective.get('allow_normal_punch', True),
                'gps': effective.get('allow_gps', True),
                'geofencing': effective.get('allow_geofencing', False),
                'qr': effective.get('allow_qr', False),
                'face_recognition': effective.get('allow_face_recognition', False),
                'biometric': effective.get('allow_biometric', False),
            },
            'effective': effective,
            'source': source_dict
        })
