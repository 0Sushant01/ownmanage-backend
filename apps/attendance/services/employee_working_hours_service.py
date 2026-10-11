import logging
from datetime import time, datetime
from typing import List, Dict, Any, Optional
from django.db import transaction
from django.core.exceptions import ValidationError
from rest_framework.exceptions import ValidationError as DRFValidationError

from apps.attendance.models import EmployeeWorkingHour, WorkScheduleDay
from apps.organization.services.policy_resolver import PolicyResolver

logger = logging.getLogger(__name__)


def parse_time(val: Any) -> Optional[time]:
    """Parse a time string (HH:MM or HH:MM:SS) or return time object directly."""
    if not val:
        return None
    if isinstance(val, time):
        return val
    val_str = str(val).strip()
    if not val_str:
        return None
    parts = val_str.split(':')
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        second = int(parts[2]) if len(parts) > 2 else 0
        return time(hour, minute, second)
    except (ValueError, IndexError):
        raise DRFValidationError(f"Invalid time format: '{val}'. Expected HH:MM.")


DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


class EmployeeWorkingHoursService:
    """
    Manages employee-specific 7-day working hours records, inheritance from centre policies,
    custom overrides, resetting, and synchronization upon centre/policy changes.
    """

    @classmethod
    def resolve_centre_schedule_defaults(cls, centre=None, business=None) -> Dict[int, Dict[str, Any]]:
        """
        Resolves effective working hours per weekday (0..6) from centre policy.
        Returns a dict mapping day_of_week (0..6) to default settings.
        """
        policy_data = PolicyResolver.get_attendance_policy(centre=centre, business=business)
        eff = policy_data.get('effective', {})

        default_start = parse_time(eff.get('office_start', '09:00'))
        default_end = parse_time(eff.get('office_end', '18:00'))
        default_break_start = parse_time(eff.get('break_start', '13:00'))
        default_break_end = parse_time(eff.get('break_end', '14:00'))

        weekly_off_days = eff.get('weekly_off_days')
        if weekly_off_days is not None and isinstance(weekly_off_days, list):
            off_day_set = set(int(x) for x in weekly_off_days if str(x).isdigit())
        else:
            legacy_off = eff.get('weekly_off', 6)
            off_day_set = {int(legacy_off)}

        daily_schedules = eff.get('daily_schedules') or {}

        defaults_by_day = {}
        for day_idx in range(7):
            day_key = str(day_idx)
            start = default_start
            end = default_end
            b_start = default_break_start
            b_end = default_break_end

            # Daily schedules override in centre policy
            if day_key in daily_schedules and isinstance(daily_schedules[day_key], dict):
                ds = daily_schedules[day_key]
                if ds.get('start'):
                    start = parse_time(ds['start'])
                if ds.get('end'):
                    end = parse_time(ds['end'])
                if ds.get('break_start'):
                    b_start = parse_time(ds['break_start'])
                if ds.get('break_end'):
                    b_end = parse_time(ds['break_end'])

            is_enabled = day_idx not in off_day_set
            defaults_by_day[day_idx] = {
                'day_of_week': day_idx,
                'is_enabled': is_enabled,
                'start_time': start if is_enabled else None,
                'end_time': end if is_enabled else None,
                'break_start': b_start if is_enabled else None,
                'break_end': b_end if is_enabled else None,
                'is_override': False,
                'configuration_source': 'CENTRE' if centre else 'ENTERPRISE',
            }

        return defaults_by_day

    @classmethod
    @transaction.atomic
    def initialize_employee_schedule(cls, employee, centre=None) -> List[EmployeeWorkingHour]:
        """
        Creates or updates exactly 7 employee-specific working-hours records.
        Preserves any active employee overrides.
        """
        if not centre:
            centre = employee.branch
        business = employee.business

        centre_defaults = cls.resolve_centre_schedule_defaults(centre=centre, business=business)
        existing_records = {
            rec.day_of_week: rec
            for rec in EmployeeWorkingHour.objects.select_for_update().filter(employee=employee)
        }

        records = []
        for day_idx in range(7):
            c_def = centre_defaults[day_idx]
            existing = existing_records.get(day_idx)

            if existing and existing.is_override:
                # Retain active override!
                records.append(existing)
            elif existing:
                # Update inherited record with latest centre defaults
                existing.is_enabled = c_def['is_enabled']
                existing.start_time = c_def['start_time']
                existing.end_time = c_def['end_time']
                existing.break_start = c_def['break_start']
                existing.break_end = c_def['break_end']
                existing.is_override = False
                existing.configuration_source = c_def['configuration_source']
                existing.save()
                records.append(existing)
            else:
                # Create brand new record
                new_rec = EmployeeWorkingHour.objects.create(
                    employee=employee,
                    day_of_week=day_idx,
                    is_enabled=c_def['is_enabled'],
                    start_time=c_def['start_time'],
                    end_time=c_def['end_time'],
                    break_start=c_def['break_start'],
                    break_end=c_def['break_end'],
                    is_override=False,
                    configuration_source=c_def['configuration_source'],
                )
                records.append(new_rec)

        return sorted(records, key=lambda x: x.day_of_week)

    @classmethod
    def get_or_initialize_schedule(cls, employee) -> Dict[str, Any]:
        """
        Guarantees that all 7 weekday records exist for the employee.
        Returns the 7 records along with configuration summary metadata.
        """
        existing = list(
            EmployeeWorkingHour.objects.filter(employee=employee).order_by('day_of_week')
        )
        if len(existing) < 7:
            existing = cls.initialize_employee_schedule(employee)

        has_any_override = any(r.is_override for r in existing)
        centre = employee.branch

        if has_any_override:
            config_source = 'Employee Custom Schedule'
            source_badge = 'Employee Override'
        elif centre:
            config_source = f"Centre Policy ({centre.name})"
            source_badge = 'Inherited from Centre'
        else:
            config_source = 'Enterprise Default Policy'
            source_badge = 'Inherited from Enterprise'

        days_data = []
        for rec in existing:
            days_data.append({
                'id': str(rec.id),
                'day_of_week': rec.day_of_week,
                'day_name': DAY_NAMES[rec.day_of_week],
                'is_enabled': rec.is_enabled,
                'start_time': rec.start_time.strftime('%H:%M') if rec.start_time else None,
                'end_time': rec.end_time.strftime('%H:%M') if rec.end_time else None,
                'break_start': rec.break_start.strftime('%H:%M') if rec.break_start else None,
                'break_end': rec.break_end.strftime('%H:%M') if rec.break_end else None,
                'is_override': rec.is_override,
                'configuration_source': rec.configuration_source,
            })

        return {
            'employee_id': str(employee.id),
            'employee_name': employee.full_name,
            'centre_id': str(centre.id) if centre else None,
            'centre_name': centre.name if centre else 'Unassigned Centre',
            'is_override': has_any_override,
            'configuration_source': config_source,
            'source_badge': source_badge,
            'days': days_data,
        }

    @classmethod
    def validate_day_schedule(cls, day_idx: int, day_data: Dict[str, Any]):
        """
        Validates shift timings and break intervals for a single weekday.
        """
        is_enabled = bool(day_data.get('is_enabled', True))
        day_name = DAY_NAMES[day_idx]

        if not is_enabled:
            return  # Disabled days do not require shift times

        start_time = parse_time(day_data.get('start_time'))
        end_time = parse_time(day_data.get('end_time'))

        if not start_time or not end_time:
            raise DRFValidationError({
                'detail': f"{day_name}: Shift start and end times are required for enabled working days."
            })

        # Unless explicitly marked as overnight, end time must be greater than start time
        is_overnight = bool(day_data.get('is_overnight', False))
        if not is_overnight and end_time <= start_time:
            raise DRFValidationError({
                'detail': f"{day_name}: Shift end time ({end_time.strftime('%H:%M')}) must be later than start time ({start_time.strftime('%H:%M')})."
            })

        break_start = parse_time(day_data.get('break_start'))
        break_end = parse_time(day_data.get('break_end'))

        if (break_start and not break_end) or (break_end and not break_start):
            raise DRFValidationError({
                'detail': f"{day_name}: Both break start and break end times must be provided if break is configured."
            })

        if break_start and break_end:
            if break_end <= break_start:
                raise DRFValidationError({
                    'detail': f"{day_name}: Break end time must be later than break start time."
                })
            # For non-overnight shifts, check break within shift
            if not is_overnight:
                if break_start < start_time or break_end > end_time:
                    raise DRFValidationError({
                        'detail': f"{day_name}: Break schedule ({break_start.strftime('%H:%M')}–{break_end.strftime('%H:%M')}) must fall within shift hours ({start_time.strftime('%H:%M')}–{end_time.strftime('%H:%M')})."
                    })

    @classmethod
    @transaction.atomic
    def update_employee_schedule(
        cls,
        employee,
        days_data: List[Dict[str, Any]],
        user=None,
        reason: Optional[str] = None
    ) -> List[EmployeeWorkingHour]:
        """
        Atomically updates all 7 employee working-hours records as custom overrides.
        Validates the entire schedule before saving any record.
        """
        if len(days_data) != 7:
            raise DRFValidationError({'detail': 'A complete 7-day schedule (Monday to Sunday) must be provided.'})

        # Map by day_of_week
        data_by_day = {}
        for item in days_data:
            d_idx = item.get('day_of_week')
            if d_idx is None or not (0 <= int(d_idx) <= 6):
                raise DRFValidationError({'detail': f'Invalid day_of_week value: {d_idx}. Must be between 0 and 6.'})
            data_by_day[int(d_idx)] = item

        if len(data_by_day) != 7:
            raise DRFValidationError({'detail': 'All 7 unique days (Monday through Sunday) must be specified.'})

        # Validate each day first
        for day_idx in range(7):
            cls.validate_day_schedule(day_idx, data_by_day[day_idx])

        # Existing records lock
        existing_records = {
            r.day_of_week: r
            for r in EmployeeWorkingHour.objects.select_for_update().filter(employee=employee)
        }

        updated_records = []
        old_data_summary = []

        for day_idx in range(7):
            d_data = data_by_day[day_idx]
            rec = existing_records.get(day_idx)

            is_enabled = bool(d_data.get('is_enabled', True))
            start_t = parse_time(d_data.get('start_time')) if is_enabled else None
            end_t = parse_time(d_data.get('end_time')) if is_enabled else None
            b_start = parse_time(d_data.get('break_start')) if is_enabled else None
            b_end = parse_time(d_data.get('break_end')) if is_enabled else None

            if rec:
                old_data_summary.append({
                    'day': DAY_NAMES[day_idx],
                    'enabled': rec.is_enabled,
                    'start': rec.start_time.strftime('%H:%M') if rec.start_time else None,
                    'end': rec.end_time.strftime('%H:%M') if rec.end_time else None,
                })
                rec.is_enabled = is_enabled
                rec.start_time = start_t
                rec.end_time = end_t
                rec.break_start = b_start
                rec.break_end = b_end
                rec.is_override = True
                rec.configuration_source = 'EMPLOYEE'
                rec.save()
                updated_records.append(rec)
            else:
                new_rec = EmployeeWorkingHour.objects.create(
                    employee=employee,
                    day_of_week=day_idx,
                    is_enabled=is_enabled,
                    start_time=start_t,
                    end_time=end_t,
                    break_start=b_start,
                    break_end=b_end,
                    is_override=True,
                    configuration_source='EMPLOYEE',
                )
                updated_records.append(new_rec)

        # Audit and activity logging
        try:
            from apps.core.services.audit_service import AuditService
            AuditService.log(
                user_or_request=user,
                action='UPDATE_EMPLOYEE_WORKING_HOURS',
                entity_type='EmployeeWorkingHour',
                entity_id=str(employee.id),
                old_data={'schedule': old_data_summary},
                new_data={'schedule': [
                    {
                        'day': DAY_NAMES[r.day_of_week],
                        'enabled': r.is_enabled,
                        'start': r.start_time.strftime('%H:%M') if r.start_time else None,
                        'end': r.end_time.strftime('%H:%M') if r.end_time else None,
                    } for r in updated_records
                ]},
                business=employee.business,
                reason=reason or f'Updated working hours schedule for {employee.full_name}'
            )
        except Exception as e:
            logger.warning("AuditService log failed: %s", e)

        try:
            from apps.organization.models import EmployeeActivityLog
            EmployeeActivityLog.objects.create(
                business=employee.business,
                employee=employee,
                activity_type='WORKING_HOURS_OVERRIDE_ENABLED',
                description=f"Working hours customized for {employee.full_name}.",
                new_value={'is_override': True},
                performed_by=user if getattr(user, 'is_authenticated', False) else None
            )
        except Exception as e:
            logger.warning("EmployeeActivityLog failed: %s", e)

        return sorted(updated_records, key=lambda x: x.day_of_week)

    @classmethod
    @transaction.atomic
    def reset_schedule_to_centre(cls, employee, user=None) -> List[EmployeeWorkingHour]:
        """
        Reverts an employee's working hours to inherit the centre's effective policy.
        Resets is_override to False and replaces customized timings with centre defaults.
        """
        centre = employee.branch
        business = employee.business
        centre_defaults = cls.resolve_centre_schedule_defaults(centre=centre, business=business)

        existing_records = {
            r.day_of_week: r
            for r in EmployeeWorkingHour.objects.select_for_update().filter(employee=employee)
        }

        updated_records = []
        for day_idx in range(7):
            c_def = centre_defaults[day_idx]
            rec = existing_records.get(day_idx)

            if rec:
                rec.is_enabled = c_def['is_enabled']
                rec.start_time = c_def['start_time']
                rec.end_time = c_def['end_time']
                rec.break_start = c_def['break_start']
                rec.break_end = c_def['break_end']
                rec.is_override = False
                rec.configuration_source = c_def['configuration_source']
                rec.save()
                updated_records.append(rec)
            else:
                new_rec = EmployeeWorkingHour.objects.create(
                    employee=employee,
                    day_of_week=day_idx,
                    is_enabled=c_def['is_enabled'],
                    start_time=c_def['start_time'],
                    end_time=c_def['end_time'],
                    break_start=c_def['break_start'],
                    break_end=c_def['break_end'],
                    is_override=False,
                    configuration_source=c_def['configuration_source'],
                )
                updated_records.append(new_rec)

        try:
            from apps.core.services.audit_service import AuditService
            AuditService.log(
                user_or_request=user,
                action='RESET_EMPLOYEE_WORKING_HOURS_TO_CENTRE',
                entity_type='EmployeeWorkingHour',
                entity_id=str(employee.id),
                old_data={'action': 'override_active'},
                new_data={'action': 'restored_centre_defaults', 'centre_id': str(centre.id) if centre else None},
                business=employee.business,
                reason=f"Restored centre working hours policy for {employee.full_name}"
            )
        except Exception as e:
            logger.warning("AuditService log failed: %s", e)

        try:
            from apps.organization.models import EmployeeActivityLog
            EmployeeActivityLog.objects.create(
                business=employee.business,
                employee=employee,
                activity_type='WORKING_HOURS_OVERRIDE_DISABLED',
                description=f"Working hours reset to centre defaults for {employee.full_name}.",
                new_value={'is_override': False, 'centre': centre.name if centre else 'Enterprise'},
                performed_by=user if getattr(user, 'is_authenticated', False) else None
            )
        except Exception as e:
            logger.warning("EmployeeActivityLog failed: %s", e)

        return sorted(updated_records, key=lambda x: x.day_of_week)

    @classmethod
    @transaction.atomic
    def sync_centre_policy_change(cls, centre) -> int:
        """
        Synchronizes working hours when a centre's attendance policy is updated or reset.
        Updates all employees in the centre who are INHERITING the centre policy (is_override=False).
        Employees with active custom overrides are safely preserved!
        """
        centre_defaults = cls.resolve_centre_schedule_defaults(centre=centre, business=centre.business)
        employees = centre.employees.filter(employment_status='ACTIVE')

        synced_count = 0
        for emp in employees:
            # Check if this employee has customized their schedule
            has_override = EmployeeWorkingHour.objects.filter(employee=emp, is_override=True).exists()
            if has_override:
                # Do NOT overwrite customized schedules!
                continue

            # Update all 7 records to match the newly updated centre policy
            existing_recs = {
                r.day_of_week: r
                for r in EmployeeWorkingHour.objects.select_for_update().filter(employee=emp)
            }
            for day_idx in range(7):
                c_def = centre_defaults[day_idx]
                rec = existing_recs.get(day_idx)
                if rec:
                    rec.is_enabled = c_def['is_enabled']
                    rec.start_time = c_def['start_time']
                    rec.end_time = c_def['end_time']
                    rec.break_start = c_def['break_start']
                    rec.break_end = c_def['break_end']
                    rec.is_override = False
                    rec.configuration_source = 'CENTRE'
                    rec.save()
                else:
                    EmployeeWorkingHour.objects.create(
                        employee=emp,
                        day_of_week=day_idx,
                        is_enabled=c_def['is_enabled'],
                        start_time=c_def['start_time'],
                        end_time=c_def['end_time'],
                        break_start=c_def['break_start'],
                        break_end=c_def['break_end'],
                        is_override=False,
                        configuration_source='CENTRE',
                    )
            synced_count += 1

        logger.info("Synchronized centre working hours for %d employees in centre %s", synced_count, centre.name)
        return synced_count

    @classmethod
    @transaction.atomic
    def sync_employee_centre_change(cls, employee, new_centre) -> bool:
        """
        Handles synchronization when an employee is moved to a different centre.
        If the employee has an active override (is_override=True), preserve it!
        If inheriting (is_override=False), re-initialize the 7 records to the new centre's defaults.
        """
        has_override = EmployeeWorkingHour.objects.filter(employee=employee, is_override=True).exists()
        if has_override:
            logger.info("Preserving custom override for %s moving to centre %s", employee.full_name, new_centre)
            return False

        # Reset records to inherit the new centre's policy
        cls.initialize_employee_schedule(employee, centre=new_centre)
        logger.info("Synchronized working hours for %s to new centre %s", employee.full_name, new_centre)
        return True
