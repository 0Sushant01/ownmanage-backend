from typing import Dict, Any, Optional
from datetime import datetime, time, timedelta
from django.utils import timezone
from apps.attendance.models import AttendanceStatus, AttendanceEventType, AttendanceDay
from apps.organization.services.policy_resolver import PolicyResolver


class AttendanceCalculationService:
    @staticmethod
    def calculate_daily_attendance(
        day: AttendanceDay,
        effective_policy: Optional[Dict[str, Any]] = None,
        save: bool = True
    ) -> Dict[str, Any]:
        """
        Authoritative calculation of employee attendance status from raw punch events.
        Applies resolved enterprise + centre attendance policies.
        """
        employee = day.employee
        centre = day.centre or employee.branch
        business = day.business

        if not effective_policy:
            policy_data = PolicyResolver.get_attendance_policy(centre=centre, business=business)
            policy = policy_data['effective']
        else:
            policy = effective_policy

        # Parse shift boundaries from policy
        office_start_str = policy.get('office_start', '09:00')
        office_end_str = policy.get('office_end', '18:00')
        grace_period = int(policy.get('grace_period_minutes', 15))
        min_present = int(policy.get('minimum_present_minutes', 480))
        min_half_day = int(policy.get('minimum_half_day_minutes', 240))
        late_threshold = int(policy.get('late_threshold_minutes', 0))
        early_checkout_threshold = int(policy.get('early_checkout_threshold_minutes', 0))
        ot_enabled = bool(policy.get('ot_enabled', False))
        ot_grace = int(policy.get('ot_grace_minutes', 30))

        # Check for daily schedules override for day-of-week
        weekday_idx = day.attendance_date.weekday()
        weekday_key = str(weekday_idx)
        daily_schedules = policy.get('daily_schedules') or {}
        if weekday_key in daily_schedules and isinstance(daily_schedules[weekday_key], dict):
            sched = daily_schedules[weekday_key]
            if sched.get('start'):
                office_start_str = sched['start']
            if sched.get('end'):
                office_end_str = sched['end']

        # Check for weekly off days (supports 0 to 7 days: e.g. [], [6], [0, 6], etc.)
        weekly_off_days = policy.get('weekly_off_days')
        if weekly_off_days is not None and isinstance(weekly_off_days, list):
            is_weekly_off = weekday_idx in [int(x) for x in weekly_off_days]
        else:
            weekly_off_day = int(policy.get('weekly_off', 6))
            is_weekly_off = (weekday_idx == weekly_off_day)

        # Resolve local timezone (Centre priority, then Enterprise, then Asia/Kolkata)
        tz_name = (centre.timezone if centre and getattr(centre, 'timezone', None) else (business.timezone if business and business.timezone else 'Asia/Kolkata'))
        try:
            import zoneinfo
            biz_tz = zoneinfo.ZoneInfo(tz_name)
        except Exception:
            import zoneinfo
            biz_tz = zoneinfo.ZoneInfo('Asia/Kolkata')

        # Gather punch events
        events = list(day.events.order_by('event_time'))
        if not events:
            # No punches recorded
            status = AttendanceStatus.WEEK_OFF if is_weekly_off else AttendanceStatus.ABSENT
            result = {
                'status': status,
                'total_work_seconds': 0,
                'overtime_seconds': 0,
                'late_minutes': 0,
                'early_leave_minutes': 0,
                'calculation_metadata': {'reason': 'No punches recorded'}
            }
            if save:
                day.status = status
                day.total_work_seconds = 0
                day.overtime_seconds = 0
                day.late_minutes = 0
                day.early_leave_minutes = 0
                day.save(update_fields=['status', 'total_work_seconds', 'overtime_seconds', 'late_minutes', 'early_leave_minutes', 'updated_at'])
            return result

        # Determine first check-in and last check-out
        first_check_in = None
        last_check_out = None
        total_seconds = 0
        current_session_start = None

        for evt in events:
            if evt.event_type == AttendanceEventType.CHECK_IN:
                if not first_check_in:
                    first_check_in = evt.event_time
                current_session_start = evt.event_time
            elif evt.event_type == AttendanceEventType.CHECK_OUT:
                last_check_out = evt.event_time
                if current_session_start:
                    session_sec = max(0, int((evt.event_time - current_session_start).total_seconds()))
                    total_seconds += session_sec
                    current_session_start = None

        # If currently checked in without checkout yet
        if current_session_start:
            now_sec = max(0, int((timezone.now() - current_session_start).total_seconds()))
            total_seconds += now_sec

        # Late arrival calculation
        late_minutes = 0
        if first_check_in:
            local_first_in = first_check_in.astimezone(biz_tz)
            try:
                start_h, start_m = [int(x) for x in str(office_start_str).split(':')[:2]]
                expected_start = datetime.combine(local_first_in.date(), time(start_h, start_m), tzinfo=biz_tz)
                grace_cutoff = expected_start + timedelta(minutes=grace_period)

                if local_first_in > grace_cutoff:
                    late_delta = local_first_in - expected_start
                    late_minutes = max(0, int(late_delta.total_seconds() / 60))
            except Exception:
                late_minutes = 0

        # Early departure calculation
        early_leave_minutes = 0
        if last_check_out:
            local_last_out = last_check_out.astimezone(biz_tz)
            try:
                end_h, end_m = [int(x) for x in str(office_end_str).split(':')[:2]]
                expected_end = datetime.combine(local_last_out.date(), time(end_h, end_m), tzinfo=biz_tz)

                if local_last_out < expected_end:
                    early_delta = expected_end - local_last_out
                    early_leave_minutes = max(0, int(early_delta.total_seconds() / 60))
            except Exception:
                early_leave_minutes = 0

        # Overtime calculation
        overtime_seconds = 0
        if ot_enabled:
            ot_seconds_candidates = []
            standard_day_seconds = min_present * 60

            # Candidate 1: Checkout past shift end time plus OT grace
            if last_check_out:
                local_last_out = last_check_out.astimezone(biz_tz)
                try:
                    end_h, end_m = [int(x) for x in str(office_end_str).split(':')[:2]]
                    expected_end = datetime.combine(local_last_out.date(), time(end_h, end_m), tzinfo=biz_tz)
                    ot_cutoff = expected_end + timedelta(minutes=ot_grace)
                    if local_last_out > ot_cutoff:
                        ot_seconds_candidates.append(int((local_last_out - expected_end).total_seconds()))
                except Exception:
                    pass

            # Candidate 2: Worked duration exceeds standard day + OT grace
            if total_seconds > (standard_day_seconds + (ot_grace * 60)):
                ot_seconds_candidates.append(total_seconds - standard_day_seconds)

            if ot_seconds_candidates:
                overtime_seconds = max(ot_seconds_candidates)
                max_ot = int(policy.get('max_daily_ot_minutes', 240)) * 60
                if max_ot > 0:
                    overtime_seconds = min(overtime_seconds, max_ot)
        else:
            overtime_seconds = 0

        # Status resolution
        worked_minutes = int(total_seconds / 60)
        is_late = (late_minutes >= late_threshold) if late_threshold > 0 else (late_minutes > 0)
        is_early_leave = (early_leave_minutes >= early_checkout_threshold) if early_checkout_threshold > 0 else (early_leave_minutes > 15)

        is_session_active = bool(current_session_start is not None)
        if is_session_active and not last_check_out:
            # Active ongoing check-in session (employee currently clocked in)
            final_status = AttendanceStatus.LATE if is_late else AttendanceStatus.PRESENT
        elif worked_minutes >= min_present:
            if is_late:
                final_status = AttendanceStatus.LATE
            elif is_early_leave:
                final_status = AttendanceStatus.LEAVE_EARLY
            elif overtime_seconds > 0:
                final_status = AttendanceStatus.OVERTIME
            else:
                final_status = AttendanceStatus.PRESENT
        elif worked_minutes >= min_half_day:
            final_status = AttendanceStatus.HALF_DAY
        else:
            final_status = AttendanceStatus.ABSENT

        first_in_evt = next((e for e in events if e.event_type == AttendanceEventType.CHECK_IN), None)
        method_used = getattr(first_in_evt, 'attendance_method', None) or day.attendance_method or 'NORMAL'
        loc_verified = any(getattr(e, 'location_verified', False) for e in events) or day.location_verified

        result = {
            'status': final_status,
            'total_work_seconds': total_seconds,
            'overtime_seconds': overtime_seconds,
            'late_minutes': late_minutes,
            'early_leave_minutes': early_leave_minutes,
            'attendance_method': method_used,
            'location_verified': loc_verified,
            'check_in': first_check_in,
            'check_out': last_check_out,
            'calculation_metadata': {
                'worked_minutes': worked_minutes,
                'min_present_minutes': min_present,
                'min_half_day_minutes': min_half_day,
                'grace_period_minutes': grace_period,
                'ot_enabled': ot_enabled,
                'ot_grace_minutes': ot_grace,
            }
        }

        if save:
            day.status = final_status
            day.total_work_seconds = total_seconds
            day.overtime_seconds = overtime_seconds
            day.late_minutes = late_minutes
            day.early_leave_minutes = early_leave_minutes
            day.attendance_method = method_used
            day.location_verified = loc_verified
            if first_check_in:
                day.check_in = first_check_in
            if last_check_out:
                day.check_out = last_check_out
            day.save(update_fields=[
                'status',
                'total_work_seconds',
                'overtime_seconds',
                'late_minutes',
                'early_leave_minutes',
                'attendance_method',
                'location_verified',
                'check_in',
                'check_out',
                'updated_at'
            ])

        return result


def calculate_attendance_status(
    centre: Optional[Any],
    date: Any,
    check_in_time: Optional[datetime] = None,
    check_out_time: Optional[datetime] = None
) -> Dict[str, Any]:
    """
    Computes attendance status for a given centre, date, and check-in/out times.
    Supports weekly off days (0 to 7 days), grace period, and shift boundaries.
    """
    policy_data = PolicyResolver.get_attendance_policy(centre=centre)
    policy = policy_data['effective']

    weekday_idx = date.weekday()
    weekly_off_days = policy.get('weekly_off_days')
    if weekly_off_days is not None and isinstance(weekly_off_days, list):
        is_weekly_off = weekday_idx in [int(x) for x in weekly_off_days]
    else:
        weekly_off_day = int(policy.get('weekly_off', 6))
        is_weekly_off = (weekday_idx == weekly_off_day)

    if not check_in_time:
        status = AttendanceStatus.WEEK_OFF if is_weekly_off else AttendanceStatus.ABSENT
        return {
            'status': status,
            'is_weekly_off': is_weekly_off,
            'is_late': False,
            'worked_seconds': 0,
            'late_minutes': 0,
        }

    office_start_str = policy.get('office_start', '09:00')
    grace_period = int(policy.get('grace_period_minutes', 15))
    min_present = int(policy.get('minimum_present_minutes', 480))
    min_half_day = int(policy.get('minimum_half_day_minutes', 240))
    late_threshold = int(policy.get('late_threshold_minutes', 0))

    start_h, start_m = [int(x) for x in str(office_start_str).split(':')[:2]]
    expected_start = datetime.combine(check_in_time.date(), time(start_h, start_m))
    grace_cutoff = expected_start + timedelta(minutes=grace_period)

    late_minutes = 0
    if check_in_time > grace_cutoff:
        late_minutes = max(0, int((check_in_time - expected_start).total_seconds() / 60))

    worked_seconds = 0
    if check_out_time:
        worked_seconds = max(0, int((check_out_time - check_in_time).total_seconds()))

    worked_minutes = int(worked_seconds / 60)
    is_late = (late_minutes >= late_threshold) if late_threshold > 0 else (late_minutes > 0)

    if worked_minutes >= min_present:
        status = AttendanceStatus.LATE if is_late else AttendanceStatus.PRESENT
    elif worked_minutes >= min_half_day:
        status = AttendanceStatus.HALF_DAY
    else:
        status = AttendanceStatus.ABSENT

    return {
        'status': status,
        'is_weekly_off': is_weekly_off,
        'is_late': is_late,
        'late_minutes': late_minutes,
        'worked_seconds': worked_seconds,
    }
