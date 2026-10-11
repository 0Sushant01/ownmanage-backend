import datetime
from decimal import Decimal
from typing import Dict, Any, List, Optional
from datetime import date, timedelta
from django.db import transaction
from django.utils import timezone

from apps.payroll.models import (
    Payroll, PayrollRun, PayrollRunStatus, PayrollStatus, SalaryRevision, SalaryStructure,
    PayrollLineItem, PayrollLineItemType, EmployeeCompensationItem,
    CompensationComponentType, CompensationCalculationType, CompensationFrequency,
    PayrollAdjustment, GenerationType, SalarySlipVisibilityPolicy,
    PayrollException, PayrollExceptionType, PayrollExceptionReviewStatus
)
from apps.organization.models import Employee, Business, Branch, Holiday
from apps.attendance.models import AttendanceDay, AttendanceStatus
from apps.leaves.models import LeaveRequest, LeaveRequestStatus
from apps.payroll.services.compensation_resolver import CompensationResolver



class PayrollCalculationService:
    @classmethod
    def evaluate_period_attendance(
        cls,
        employee: Employee,
        period_start: date,
        period_end: date,
        daily_rate: Decimal,
        eff_att_policy: Dict[str, Any],
        persist_synthesized: bool = True
    ) -> Dict[str, Any]:
        """
        Policy-driven attendance evaluation per calendar date in [period_start, period_end].
        Rules:
        1. Employment tenure boundaries: Dates before employee.joining_date or after employee.date_of_exit are ignored (never marked absent).
        2. Holidays: Public holidays observed by employee's centre require no attendance and incur no absence deduction.
           - If worked, proposed additional compensation (HOLIDAY_WORK) is flagged for review (AWAITING_REVIEW) and not auto-paid.
        3. Weekly Offs: Configured non-working days (from EmployeeWorkingHour or policy weekly_off_days) incur no deduction.
           - If worked, proposed additional compensation (WEEK_OFF_WORK) is flagged for review (AWAITING_REVIEW).
        4. Leaves:
           - Approved Paid Leave: ON_LEAVE, no deduction.
           - Approved Unpaid Leave: ON_LEAVE, unpaid-leave deduction (no duplicate absence deduction).
           - Pending Leave: PENDING_LEAVE exception flagged for review.
        5. Attendance records:
           - PRESENT / OVERTIME: Full pay.
           - LATE: Attendance is strictly LATE (distinct from HALF_DAY). If late deduction enabled in policy, proposed deduction (default 0.5 * daily_rate) flagged AWAITING_REVIEW.
           - HALF_DAY: Attendance is HALF_DAY. Half-day deduction (default 0.5 * daily_rate). No full-day deduction.
           - ABSENT: Full-day deduction.
        6. Missing attendance on scheduled working day:
           - If no attendance record exists and not holiday, weekly off, or approved leave: Treated as ABSENT by default with full-day deduction.
           - AttendanceDay is synthesized/persisted with status=ABSENT so daily register and payroll remain 100% synchronized.
        7. Mutual exclusivity: Each calendar date produces at most ONE deduction category.
        """
        from apps.attendance.models import EmployeeWorkingHour
        wh_records = {
            wh.day_of_week: wh
            for wh in EmployeeWorkingHour.objects.filter(employee=employee)
        }

        # Holidays
        holidays_qs = Holiday.objects.filter(
            business=employee.business,
            holiday_date__gte=period_start,
            holiday_date__lte=period_end
        ).prefetch_related('centres')
        holiday_map = {}
        for h in holidays_qs:
            if h.applies_to_all_centres or (employee.branch and employee.branch in h.centres.all()):
                holiday_map[h.holiday_date] = h

        # Leaves
        leaves_qs = LeaveRequest.objects.filter(
            employee=employee,
            start_date__lte=period_end,
            end_date__gte=period_start,
            status__in=[LeaveRequestStatus.APPROVED, LeaveRequestStatus.PENDING]
        ).select_related('leave_type')
        leave_map = {}
        for lr in leaves_qs:
            curr = max(lr.start_date, period_start)
            fin = min(lr.end_date, period_end)
            while curr <= fin:
                leave_map[curr] = lr
                curr += timedelta(days=1)

        # Existing AttendanceDay records
        att_days_map = {
            day.attendance_date: day
            for day in AttendanceDay.objects.filter(
                employee=employee,
                attendance_date__gte=period_start,
                attendance_date__lte=period_end
            )
        }

        # Configuration thresholds from policy
        late_enabled = eff_att_policy.get('late_deduction_enabled', True)
        late_fraction = Decimal(str(eff_att_policy.get('late_deduction_fraction', 0.5)))
        half_day_fraction = Decimal(str(eff_att_policy.get('half_day_deduction_fraction', 0.5)))
        holiday_work_multiplier = Decimal(str(eff_att_policy.get('holiday_work_multiplier', 1.0)))
        weekly_off_work_multiplier = Decimal(str(eff_att_policy.get('weekly_off_work_multiplier', 1.0)))
        weekly_off_days = eff_att_policy.get('weekly_off_days') or [6]

        exceptions: List[Dict[str, Any]] = []
        present_count = 0
        half_day_count = 0
        weekly_off_count = 0
        holiday_count = 0
        absent_count = 0
        late_count = 0
        paid_leave_days = Decimal('0.0')
        unpaid_leave_days = Decimal('0.0')
        holiday_work_count = 0
        week_off_work_count = 0
        total_ot_seconds = 0

        curr_date = period_start
        while curr_date <= period_end:
            # Rule 1: Outside employment dates check
            if employee.joining_date and curr_date < employee.joining_date:
                curr_date += timedelta(days=1)
                continue
            if employee.date_of_exit and curr_date > employee.date_of_exit:
                curr_date += timedelta(days=1)
                continue

            wh = wh_records.get(curr_date.weekday())
            if wh:
                is_weekly_off = not wh.is_enabled
                sched_hours = Decimal('8.00')
                if wh.start_time and wh.end_time:
                    try:
                        t_diff = (datetime.datetime.combine(curr_date, wh.end_time) - datetime.datetime.combine(curr_date, wh.start_time)).total_seconds() / 3600.0
                        if t_diff > 0:
                            sched_hours = Decimal(str(round(t_diff, 2)))
                    except Exception:
                        pass
            else:
                is_weekly_off = (curr_date.weekday() in weekly_off_days)
                sched_hours = Decimal('8.00')

            is_holiday = curr_date in holiday_map
            holiday_obj = holiday_map.get(curr_date)
            lr = leave_map.get(curr_date)
            att_day = att_days_map.get(curr_date)

            if att_day:
                total_ot_seconds += getattr(att_day, 'overtime_seconds', 0)
                actual_hours = Decimal(str(round(getattr(att_day, 'total_work_seconds', 0) / 3600.0, 2))) if getattr(att_day, 'total_work_seconds', 0) > 0 else None

                if att_day.status == AttendanceStatus.PRESENT or att_day.status == AttendanceStatus.OVERTIME:
                    present_count += 1
                    if is_holiday:
                        holiday_work_count += 1
                        add_amt = (daily_rate * holiday_work_multiplier).quantize(Decimal('0.01'))
                        exceptions.append({
                            'attendance_date': curr_date,
                            'attendance_status': att_day.status,
                            'scheduled_hours': sched_hours,
                            'actual_hours': actual_hours,
                            'exception_type': PayrollExceptionType.HOLIDAY_WORK,
                            'exception_reason': f"Work on Public Holiday: {holiday_obj.name if holiday_obj else 'Holiday'}",
                            'proposed_amount': add_amt,
                            'is_deduction': False,
                            'policy_applied': f"Holiday Multiplier: {holiday_work_multiplier}x",
                            'review_status': PayrollExceptionReviewStatus.AWAITING_REVIEW,
                            'is_applied_to_payroll': False,  # Not auto-paid; requires manager approval
                        })
                    elif is_weekly_off:
                        week_off_work_count += 1
                        add_amt = (daily_rate * weekly_off_work_multiplier).quantize(Decimal('0.01'))
                        exceptions.append({
                            'attendance_date': curr_date,
                            'attendance_status': att_day.status,
                            'scheduled_hours': sched_hours,
                            'actual_hours': actual_hours,
                            'exception_type': PayrollExceptionType.WEEK_OFF_WORK,
                            'exception_reason': "Work on Weekly Off",
                            'proposed_amount': add_amt,
                            'is_deduction': False,
                            'policy_applied': f"Weekly Off Multiplier: {weekly_off_work_multiplier}x",
                            'review_status': PayrollExceptionReviewStatus.AWAITING_REVIEW,
                            'is_applied_to_payroll': False,  # Not auto-paid; requires manager approval
                        })
                elif att_day.status == AttendanceStatus.LATE:
                    present_count += 1
                    late_count += 1
                    if late_enabled:
                        ded_amt = (daily_rate * late_fraction).quantize(Decimal('0.01'))
                        exceptions.append({
                            'attendance_date': curr_date,
                            'attendance_status': AttendanceStatus.LATE,
                            'scheduled_hours': sched_hours,
                            'actual_hours': actual_hours,
                            'exception_type': PayrollExceptionType.LATE,
                            'exception_reason': f"Late arrival ({getattr(att_day, 'late_minutes', 0)}m late)",
                            'proposed_amount': ded_amt,
                            'is_deduction': True,
                            'policy_applied': f"Late Deduction Fraction: {late_fraction}",
                            'review_status': PayrollExceptionReviewStatus.AWAITING_REVIEW,
                            'is_applied_to_payroll': True,
                        })
                elif att_day.status == AttendanceStatus.HALF_DAY:
                    half_day_count += 1
                    ded_amt = (daily_rate * half_day_fraction).quantize(Decimal('0.01'))
                    exceptions.append({
                        'attendance_date': curr_date,
                        'attendance_status': AttendanceStatus.HALF_DAY,
                        'scheduled_hours': sched_hours,
                        'actual_hours': actual_hours,
                        'exception_type': PayrollExceptionType.HALF_DAY,
                        'exception_reason': "Half-Day Attendance recorded",
                        'proposed_amount': ded_amt,
                        'is_deduction': True,
                        'policy_applied': f"Half Day Deduction Fraction: {half_day_fraction}",
                        'review_status': PayrollExceptionReviewStatus.CALCULATED,
                        'is_applied_to_payroll': True,
                    })
                elif att_day.status == AttendanceStatus.ABSENT:
                    absent_count += 1
                    exceptions.append({
                        'attendance_date': curr_date,
                        'attendance_status': AttendanceStatus.ABSENT,
                        'scheduled_hours': sched_hours,
                        'actual_hours': Decimal('0.00'),
                        'exception_type': PayrollExceptionType.ABSENT,
                        'exception_reason': "Marked Absent",
                        'proposed_amount': daily_rate.quantize(Decimal('0.01')),
                        'is_deduction': True,
                        'policy_applied': "Full Day Absence Deduction",
                        'review_status': PayrollExceptionReviewStatus.CALCULATED,
                        'is_applied_to_payroll': True,
                    })
                elif att_day.status == AttendanceStatus.LEAVE:
                    if lr and lr.status == LeaveRequestStatus.APPROVED:
                        if lr.leave_type and lr.leave_type.is_paid:
                            paid_leave_days += Decimal('0.5') if lr.duration_type in ['FIRST_HALF', 'SECOND_HALF'] else Decimal('1.0')
                        else:
                            unpaid_fraction = Decimal('0.5') if lr.duration_type in ['FIRST_HALF', 'SECOND_HALF'] else Decimal('1.0')
                            unpaid_leave_days += unpaid_fraction
                            ded_amt = (daily_rate * unpaid_fraction).quantize(Decimal('0.01'))
                            exceptions.append({
                                'attendance_date': curr_date,
                                'attendance_status': AttendanceStatus.LEAVE,
                                'scheduled_hours': sched_hours,
                                'actual_hours': Decimal('0.00'),
                                'exception_type': PayrollExceptionType.UNPAID_LEAVE,
                                'exception_reason': f"Approved Unpaid Leave ({lr.leave_type.name if lr.leave_type else 'Unpaid'})",
                                'proposed_amount': ded_amt,
                                'is_deduction': True,
                                'policy_applied': f"Unpaid Leave Fraction: {unpaid_fraction}",
                                'review_status': PayrollExceptionReviewStatus.CALCULATED,
                                'is_applied_to_payroll': True,
                            })
                    else:
                        paid_leave_days += Decimal('1.0')
                elif att_day.status == AttendanceStatus.WEEK_OFF:
                    weekly_off_count += 1
                elif att_day.status == AttendanceStatus.HOLIDAY:
                    holiday_count += 1
            else:
                # No attendance record exists for curr_date
                if is_holiday:
                    holiday_count += 1
                elif is_weekly_off:
                    weekly_off_count += 1
                elif lr:
                    if lr.status == LeaveRequestStatus.APPROVED:
                        if lr.leave_type and lr.leave_type.is_paid:
                            paid_leave_days += Decimal('0.5') if lr.duration_type in ['FIRST_HALF', 'SECOND_HALF'] else Decimal('1.0')
                        else:
                            unpaid_fraction = Decimal('0.5') if lr.duration_type in ['FIRST_HALF', 'SECOND_HALF'] else Decimal('1.0')
                            unpaid_leave_days += unpaid_fraction
                            ded_amt = (daily_rate * unpaid_fraction).quantize(Decimal('0.01'))
                            exceptions.append({
                                'attendance_date': curr_date,
                                'attendance_status': AttendanceStatus.LEAVE,
                                'scheduled_hours': sched_hours,
                                'actual_hours': Decimal('0.00'),
                                'exception_type': PayrollExceptionType.UNPAID_LEAVE,
                                'exception_reason': f"Approved Unpaid Leave ({lr.leave_type.name if lr.leave_type else 'Unpaid'})",
                                'proposed_amount': ded_amt,
                                'is_deduction': True,
                                'policy_applied': f"Unpaid Leave Fraction: {unpaid_fraction}",
                                'review_status': PayrollExceptionReviewStatus.CALCULATED,
                                'is_applied_to_payroll': True,
                            })
                    elif lr.status == LeaveRequestStatus.PENDING:
                        absent_count += 1
                        exceptions.append({
                            'attendance_date': curr_date,
                            'attendance_status': AttendanceStatus.ABSENT,
                            'scheduled_hours': sched_hours,
                            'actual_hours': Decimal('0.00'),
                            'exception_type': PayrollExceptionType.PENDING_LEAVE,
                            'exception_reason': f"Unresolved pending leave ({lr.leave_type.name if lr.leave_type else 'Leave'})",
                            'proposed_amount': daily_rate.quantize(Decimal('0.01')),
                            'is_deduction': True,
                            'policy_applied': "Pending Leave Treated as Unresolved Absence",
                            'review_status': PayrollExceptionReviewStatus.AWAITING_REVIEW,
                            'is_applied_to_payroll': True,
                        })
                        if persist_synthesized:
                            AttendanceDay.objects.get_or_create(
                                business=employee.business,
                                employee=employee,
                                attendance_date=curr_date,
                                defaults={'centre': employee.branch, 'status': AttendanceStatus.ABSENT, 'total_work_seconds': 0}
                            )
                else:
                    # Scheduled working day + No attendance record + No approved leave = ABSENT BY DEFAULT
                    absent_count += 1
                    exceptions.append({
                        'attendance_date': curr_date,
                        'attendance_status': AttendanceStatus.ABSENT,
                        'scheduled_hours': sched_hours,
                        'actual_hours': Decimal('0.00'),
                        'exception_type': PayrollExceptionType.ABSENT,
                        'exception_reason': "Missing attendance on scheduled working day (treated as absent by default)",
                        'proposed_amount': daily_rate.quantize(Decimal('0.01')),
                        'is_deduction': True,
                        'policy_applied': "Absent by Default",
                        'review_status': PayrollExceptionReviewStatus.CALCULATED,
                        'is_applied_to_payroll': True,
                    })
                    if persist_synthesized:
                        AttendanceDay.objects.get_or_create(
                            business=employee.business,
                            employee=employee,
                            attendance_date=curr_date,
                            defaults={'centre': employee.branch, 'status': AttendanceStatus.ABSENT, 'total_work_seconds': 0}
                        )

            curr_date += timedelta(days=1)

        ot_hours = Decimal(str(round(total_ot_seconds / 3600.0, 2)))

        # Build attendance line items
        attendance_line_items: List[Dict[str, Any]] = []

        # Deductions
        absent_excs = [e for e in exceptions if e['exception_type'] in [PayrollExceptionType.ABSENT, PayrollExceptionType.PENDING_LEAVE] and e['is_applied_to_payroll']]
        if absent_excs:
            tot_amt = sum(e['proposed_amount'] for e in absent_excs)
            attendance_line_items.append({
                'name': f"Absence Deduction ({len(absent_excs)} days)",
                'line_type': PayrollLineItemType.DEDUCTION,
                'amount': tot_amt,
                'rate': daily_rate,
                'units': Decimal(str(len(absent_excs))),
                'is_deduction': True,
                'source_compensation_item': None,
            })

        late_excs = [e for e in exceptions if e['exception_type'] == PayrollExceptionType.LATE and e['is_applied_to_payroll']]
        if late_excs:
            tot_amt = sum(e['proposed_amount'] for e in late_excs)
            attendance_line_items.append({
                'name': f"Late Arrival Deduction ({len(late_excs)} days)",
                'line_type': PayrollLineItemType.DEDUCTION,
                'amount': tot_amt,
                'rate': (daily_rate * late_fraction).quantize(Decimal('0.01')),
                'units': Decimal(str(len(late_excs))),
                'is_deduction': True,
                'source_compensation_item': None,
            })

        half_excs = [e for e in exceptions if e['exception_type'] == PayrollExceptionType.HALF_DAY and e['is_applied_to_payroll']]
        if half_excs:
            tot_amt = sum(e['proposed_amount'] for e in half_excs)
            attendance_line_items.append({
                'name': f"Half-Day Deduction ({len(half_excs)} days)",
                'line_type': PayrollLineItemType.DEDUCTION,
                'amount': tot_amt,
                'rate': (daily_rate * half_day_fraction).quantize(Decimal('0.01')),
                'units': Decimal(str(len(half_excs))),
                'is_deduction': True,
                'source_compensation_item': None,
            })

        unpaid_excs = [e for e in exceptions if e['exception_type'] == PayrollExceptionType.UNPAID_LEAVE and e['is_applied_to_payroll']]
        if unpaid_excs:
            tot_amt = sum(e['proposed_amount'] for e in unpaid_excs)
            attendance_line_items.append({
                'name': f"Unpaid Leave ({len(unpaid_excs)} days)",
                'line_type': PayrollLineItemType.UNPAID_LEAVE,
                'amount': tot_amt,
                'rate': daily_rate,
                'units': unpaid_leave_days,
                'is_deduction': True,
                'source_compensation_item': None,
            })

        # Additions (holiday / week-off work) - only if is_applied_to_payroll is True
        applied_hw = [e for e in exceptions if e['exception_type'] in [PayrollExceptionType.HOLIDAY_WORK, PayrollExceptionType.WEEK_OFF_WORK] and e['is_applied_to_payroll']]
        if applied_hw:
            tot_amt = sum(e['proposed_amount'] for e in applied_hw)
            attendance_line_items.append({
                'name': f"Holiday / Week-Off Work ({len(applied_hw)} days)",
                'line_type': PayrollLineItemType.EARNING,
                'amount': tot_amt,
                'rate': daily_rate,
                'units': Decimal(str(len(applied_hw))),
                'is_deduction': False,
                'source_compensation_item': None,
            })

        return {
            'present_count': present_count,
            'half_day_count': half_day_count,
            'weekly_off_count': weekly_off_count,
            'holiday_count': holiday_count,
            'absent_count': absent_count,
            'late_count': late_count,
            'paid_leave_days': paid_leave_days,
            'unpaid_leave_days': unpaid_leave_days,
            'holiday_work_count': holiday_work_count,
            'week_off_work_count': week_off_work_count,
            'ot_hours': ot_hours,
            'exceptions': exceptions,
            'line_items': attendance_line_items,
        }

    @classmethod
    def calculate_employee_payroll(
        cls,
        employee: Employee,
        period_start: date,
        period_end: date
    ) -> Dict[str, Any]:

        """
        Calculates itemized remuneration for an employee over a specified pay period.
        Integrates normalized EmployeeCompensationItem records (one-time vs recurring),
        evaluates attendance, and generates detailed line items.
        Formula: Base Salary + Allowances + Bonuses + Overtime - Unpaid Leaves - Deductions = Net Pay
        """
        business = employee.business

        # 1. Fetch active compensation profile (SalaryRevision prioritized over legacy SalaryStructure)
        revision = SalaryRevision.objects.filter(
            employee=employee,
            effective_from__lte=period_end
        ).order_by('-effective_from').first()

        basic_salary = Decimal('0.00')
        currency = 'INR'
        hourly_rate = Decimal('0.00')
        ot_rate = Decimal('0.00')
        legacy_allowances = {}
        legacy_deductions = {}
        is_prorated = False
        proration_details = {}

        total_calendar_days = (period_end - period_start).days + 1

        if revision:
            currency = revision.currency
            hourly_rate = Decimal(str(revision.hourly_rate))
            ot_rate = Decimal(str(revision.ot_rate))
            legacy_allowances = revision.allowances or {}
            legacy_deductions = revision.deduction_rules or {}

            # Check for mid-period salary revision proration
            if revision.effective_from > period_start:
                prev_rev = SalaryRevision.objects.filter(
                    employee=employee,
                    effective_from__lt=revision.effective_from
                ).order_by('-effective_from').first()

                if prev_rev and total_calendar_days > 0:
                    days_pre = (revision.effective_from - period_start).days
                    days_post = (period_end - revision.effective_from).days + 1
                    pre_salary = Decimal(str(prev_rev.basic_salary))
                    post_salary = Decimal(str(revision.basic_salary))

                    basic_salary = (
                        (pre_salary * Decimal(days_pre) / Decimal(total_calendar_days)) +
                        (post_salary * Decimal(days_post) / Decimal(total_calendar_days))
                    ).quantize(Decimal('0.01'))

                    is_prorated = True
                    proration_details = {
                        'is_prorated': True,
                        'pre_revision_salary': float(pre_salary),
                        'pre_days': days_pre,
                        'post_revision_salary': float(post_salary),
                        'post_days': days_post,
                        'total_period_days': total_calendar_days,
                        'revision_effective_from': str(revision.effective_from),
                    }
                else:
                    basic_salary = Decimal(str(revision.basic_salary))
            else:
                basic_salary = Decimal(str(revision.basic_salary))
        else:
            legacy = SalaryStructure.objects.filter(
                employee=employee,
                effective_from__lte=period_end
            ).order_by('-effective_from').first()

            if legacy:
                basic_salary = Decimal(str(legacy.basic_salary))
                currency = legacy.currency
                for comp in legacy.components.all():
                    if comp.component_type == 'EARNING':
                        legacy_allowances[comp.name] = float(comp.amount)
                    else:
                        legacy_deductions[comp.name] = float(comp.amount)

        total_calendar_days = (period_end - period_start).days + 1
        effective_working_days = 30  # Standard payroll convention

        # Resolve effective payroll schedule & compensation type
        from apps.payroll.services.payroll_schedule_service import PayrollScheduleService
        schedule_res = PayrollScheduleService.resolve_schedule(employee=employee)
        eff_cfg = schedule_res['effective_config']
        comp_type = eff_cfg.get('compensation_type', 'MONTHLY_SALARY')

        # Resolve effective attendance policy for working-hours, grace period, and deductions
        from apps.organization.services.policy_resolver import PolicyResolver
        att_policy_data = PolicyResolver.get_attendance_policy(centre=employee.branch, business=business)
        eff_att_policy = att_policy_data.get('effective', {})
        policy_ot_multiplier = Decimal(str(eff_att_policy.get('ot_rate_multiplier') or '1.5'))

        # Compute base daily rate respecting compensation type and salary revision unit
        rev_unit = (getattr(revision, 'salary_unit', None) or '').strip()
        if not rev_unit:
            rev_unit = 'DAILY' if comp_type == 'DAILY_WAGE' else 'MONTHLY'

        divisor = Decimal(str(eff_att_policy.get('absence_deduction_divisor', 30)))
        if divisor <= Decimal('0.00'):
            divisor = Decimal('30.00')

        if rev_unit == 'DAILY':
            daily_rate = basic_salary
        elif rev_unit == 'WEEKLY':
            daily_rate = (basic_salary / Decimal('7.00')).quantize(Decimal('0.01'))
        elif comp_type == 'DAILY_WAGE' and not getattr(revision, 'salary_unit', None):
            daily_rate = basic_salary
        else:
            daily_rate = (basic_salary / divisor).quantize(Decimal('0.01'))

        # 2. Policy-driven Attendance and Exceptions Evaluation
        eval_res = cls.evaluate_period_attendance(
            employee=employee,
            period_start=period_start,
            period_end=period_end,
            daily_rate=daily_rate,
            eff_att_policy=eff_att_policy,
            persist_synthesized=True
        )

        present_count = eval_res['present_count']
        half_day_count = eval_res['half_day_count']
        weekly_off_count = eval_res['weekly_off_count']
        holiday_count = eval_res['holiday_count']
        absent_count = eval_res['absent_count']
        late_count = eval_res['late_count']
        paid_leave_days = eval_res['paid_leave_days']
        unpaid_leave_days = eval_res['unpaid_leave_days']
        ot_hours = eval_res['ot_hours']
        exceptions_data = eval_res['exceptions']
        att_line_items = eval_res['line_items']

        paid_days_effective = Decimal(str(present_count + weekly_off_count + holiday_count)) + (Decimal(str(half_day_count)) * Decimal('0.5')) + paid_leave_days

        # List of line items to build
        line_items_data: List[Dict[str, Any]] = []

        if comp_type == 'DAILY_WAGE':
            base_salary_earned = (daily_rate * paid_days_effective).quantize(Decimal('0.01'))
            line_items_data.append({
                'name': f'Daily Wages ({paid_days_effective} days)',
                'line_type': PayrollLineItemType.BASIC,
                'amount': base_salary_earned,
                'rate': daily_rate,
                'units': paid_days_effective,
                'is_deduction': False,
                'source_compensation_item': None,
            })
            base_for_gross = base_salary_earned
            skip_attendance_deductions = True
        elif comp_type == 'HOURLY_WAGE':
            eff_hourly = hourly_rate if hourly_rate > 0 else (basic_salary / Decimal('240.00')).quantize(Decimal('0.01'))
            hours_worked = ot_hours if ot_hours > 0 else (paid_days_effective * Decimal('8.00'))
            base_salary_earned = (eff_hourly * hours_worked).quantize(Decimal('0.01'))
            line_items_data.append({
                'name': f'Hourly Wages ({hours_worked} hrs)',
                'line_type': PayrollLineItemType.BASIC,
                'amount': base_salary_earned,
                'rate': eff_hourly,
                'units': hours_worked,
                'is_deduction': False,
                'source_compensation_item': None,
            })
            base_for_gross = base_salary_earned
            skip_attendance_deductions = True
        elif comp_type == 'FIXED_CONTRACT':
            line_items_data.append({
                'name': 'Fixed Contract Payment',
                'line_type': PayrollLineItemType.BASIC,
                'amount': basic_salary,
                'rate': basic_salary,
                'units': Decimal('1.00'),
                'is_deduction': False,
                'source_compensation_item': None,
            })
            base_for_gross = basic_salary
            skip_attendance_deductions = False
        else:
            # MONTHLY_SALARY
            if total_calendar_days < 28:
                # Proportional calculation for sub-monthly periods (DAILY 1-day or WEEKLY 7-day cycles)
                payable_units = min(paid_days_effective, Decimal(str(total_calendar_days)))
                base_salary_earned = (daily_rate * payable_units).quantize(Decimal('0.01'))
                period_label = 'Daily Base Salary' if total_calendar_days == 1 else f'Weekly Base Salary ({total_calendar_days} days)'
                line_items_data.append({
                    'name': period_label,
                    'line_type': PayrollLineItemType.BASIC,
                    'amount': base_salary_earned,
                    'rate': daily_rate,
                    'units': payable_units,
                    'is_deduction': False,
                    'source_compensation_item': None,
                })
                base_for_gross = base_salary_earned
                skip_attendance_deductions = True
            else:
                line_items_data.append({
                    'name': 'Base Salary',
                    'line_type': PayrollLineItemType.BASIC,
                    'amount': basic_salary,
                    'rate': daily_rate,
                    'units': Decimal(str(effective_working_days)),
                    'is_deduction': False,
                    'source_compensation_item': None,
                })
                base_for_gross = basic_salary
                skip_attendance_deductions = False

        # Overtime calculation
        if ot_rate > 0:
            eff_ot_rate = ot_rate
        elif hourly_rate > 0:
            eff_ot_rate = (hourly_rate * policy_ot_multiplier).quantize(Decimal('0.01'))
        elif basic_salary > 0:
            norm_hourly = (basic_salary / Decimal('240.00')).quantize(Decimal('0.01'))
            eff_ot_rate = (norm_hourly * policy_ot_multiplier).quantize(Decimal('0.01'))
        else:
            eff_ot_rate = Decimal('0.00')

        ot_amount = (ot_hours * eff_ot_rate).quantize(Decimal('0.01'))

        if ot_amount > Decimal('0.00'):
            line_items_data.append({
                'name': f'Overtime ({ot_hours} hrs @ {eff_ot_rate}/hr)',
                'line_type': PayrollLineItemType.OVERTIME,
                'amount': ot_amount,
                'rate': eff_ot_rate,
                'units': ot_hours,
                'is_deduction': False,
                'source_compensation_item': None,
            })

        # Check if employee's compensation type is DAILY
        is_daily_wage = (comp_type == 'DAILY_WAGE' or rev_unit == 'DAILY')

        total_comp_earnings = Decimal('0.00')
        total_comp_deductions = Decimal('0.00')
        comp_items_snapshot = []

        if not is_daily_wage:
            # 4. Itemized Compensation Components evaluation via CompensationResolver
            # Enforces Affect Payroll ON/OFF, One-Time vs Recurring rules, proration, and inheritance
            eligible_items = CompensationResolver.get_eligible_components_for_payroll(
                employee=employee,
                period_start=period_start,
                period_end=period_end,
                basic_salary=basic_salary
            )

            for c in eligible_items:
                item = c['item']
                comp_amt = c['calculated_amount']
                is_ded = c['is_deduction']

                type_map = {
                    CompensationComponentType.EARNING: PayrollLineItemType.EARNING,
                    CompensationComponentType.ALLOWANCE: PayrollLineItemType.ALLOWANCE,
                    CompensationComponentType.BONUS: PayrollLineItemType.BONUS,
                    CompensationComponentType.DEDUCTION: PayrollLineItemType.DEDUCTION,
                    CompensationComponentType.OVERTIME: PayrollLineItemType.OVERTIME,
                }
                mapped_line_type = type_map.get(item.component_type, PayrollLineItemType.OTHER)

                line_items_data.append({
                    'name': item.name,
                    'line_type': mapped_line_type,
                    'amount': comp_amt,
                    'rate': comp_amt,
                    'units': Decimal('1.00'),
                    'is_deduction': is_ded,
                    'source_compensation_item': item,
                })

                if is_ded:
                    total_comp_deductions += comp_amt
                else:
                    total_comp_earnings += comp_amt

                comp_items_snapshot.append({
                    'id': str(item.id),
                    'name': item.name,
                    'component_type': item.component_type,
                    'calculation_type': item.calculation_type,
                    'recurrence_type': c['recurrence_type'],
                    'recurrence_frequency': c['recurrence_frequency'],
                    'configured_amount': float(item.amount),
                    'calculated_amount': float(comp_amt),
                    'is_prorated': c['is_prorated'],
                    'is_deduction': is_ded,
                    'source': c['source'],
                    'source_display': c['source_display'],
                    'affects_payroll': item.affects_payroll,
                    'effective_from': str(item.effective_from),
                    'effective_to': str(item.effective_to) if item.effective_to else None,
                })

            # 5. Legacy JSON fallback for any unmigrated rules (only for non-daily wage)
            for k, v in legacy_allowances.items():
                if not any(item['name'].lower() == k.lower() for item in line_items_data):
                    val = Decimal(str(v))
                    total_comp_earnings += val
                    line_items_data.append({
                        'name': k,
                        'line_type': PayrollLineItemType.ALLOWANCE,
                        'amount': val,
                        'rate': val,
                        'units': Decimal('1.00'),
                        'is_deduction': False,
                        'source_compensation_item': None,
                    })

            for k, v in legacy_deductions.items():
                if not any(item['name'].lower() == k.lower() for item in line_items_data):
                    val = Decimal(str(v))
                    total_comp_deductions += val
                    line_items_data.append({
                        'name': k,
                        'line_type': PayrollLineItemType.DEDUCTION,
                        'amount': val,
                        'rate': val,
                        'units': Decimal('1.00'),
                        'is_deduction': True,
                        'source_compensation_item': None,
                    })

        # Add attendance exception line items (Absences, Late arrivals, Half-Days, Unpaid Leaves, Holiday Additions)
        total_att_additions = Decimal('0.00')
        total_att_deductions = Decimal('0.00')

        if not skip_attendance_deductions:
            for item in att_line_items:
                line_items_data.append(item)
                if item['is_deduction']:
                    total_att_deductions += item['amount']
                else:
                    total_att_additions += item['amount']
        else:
            for item in att_line_items:
                if not item['is_deduction']:
                    line_items_data.append(item)
                    total_att_additions += item['amount']
            for exc in exceptions_data:
                if exc.get('is_deduction'):
                    exc['is_applied_to_payroll'] = False
                    exc['proposed_amount'] = Decimal('0.00')

        gross_amount = base_for_gross + total_comp_earnings + ot_amount + total_att_additions
        total_deductions = total_comp_deductions + total_att_deductions
        net_amount = max(Decimal('0.00'), gross_amount - total_deductions)

        return {
            'employee_id': str(employee.id),
            'employee_name': employee.full_name,
            'period_start': period_start,
            'period_end': period_end,
            'working_days': total_calendar_days,
            'paid_days': float(paid_days_effective),
            'unpaid_days': float(unpaid_leave_days),
            'half_days': half_day_count,
            'ot_hours': float(ot_hours),
            'gross_amount': gross_amount,
            'total_deductions': total_deductions,
            'net_amount': net_amount,
            'currency': currency,
            'line_items': line_items_data,
            'exceptions_data': exceptions_data,
            'attendance_summary': {
                'present_count': present_count,
                'half_day_count': half_day_count,
                'weekly_off_count': weekly_off_count,
                'holiday_count': holiday_count,
                'absent_count': absent_count,
                'late_count': late_count,
                'paid_leave_days': float(paid_leave_days),
                'unpaid_leave_days': float(unpaid_leave_days),
                'exceptions_count': len(exceptions_data),
            },
            'compensation_type': comp_type,
            'salary_unit': rev_unit,
            'base_salary_amount': basic_salary,
            'salary_snapshot': {
                'basic_salary': float(basic_salary),
                'ot_amount': float(ot_amount),
                'attendance_deductions': float(total_att_deductions),
                'attendance_additions': float(total_att_additions),
                'total_comp_earnings': float(total_comp_earnings),
                'total_comp_deductions': float(total_comp_deductions),
                'is_prorated': is_prorated,
                'proration_details': proration_details,
                'is_daily_wage': is_daily_wage,
                'itemized_components_notice': (
                    'Itemized compensation components are not applicable to daily-wage payroll.'
                    if is_daily_wage else None
                ),
                'comp_items_snapshot': comp_items_snapshot,
            },
            'schedule_snapshot': {
                'source': schedule_res['source'],
                'source_display': schedule_res['source_display'],
                'compensation_type': comp_type,
                'pay_frequency': eff_cfg.get('pay_frequency'),
                'generation_mode': eff_cfg.get('generation_mode'),
                'payment_rule': eff_cfg.get('payment_rule'),
                'expected_payment_date': schedule_res.get('expected_payment_date'),
            }
        }

    @classmethod
    def run_batch_payroll(
        cls,
        business: Business,
        period_start: date,
        period_end: date,
        centre: Optional[Branch] = None,
        approved_by=None,
        user=None
    ) -> PayrollRun:
        if user and not approved_by:
            approved_by = user
        """
        Executes a batch payroll run across all active employees in an enterprise or center.
        Creates both the summary Payroll record AND itemized PayrollLineItem rows in a single atomic transaction.
        Enforces immutability: Finalized payroll runs and paid employee records can never be overwritten.
        """
        from rest_framework.exceptions import ValidationError
        from apps.payroll.services.payroll_schedule_service import PayrollScheduleService

        # 1. Protection: Check if a finalized run exists for this period
        existing_run = PayrollRun.objects.filter(
            business=business,
            centre=centre,
            period_start=period_start,
            period_end=period_end
        ).first()

        if existing_run and existing_run.status in [PayrollRunStatus.FINALIZED, PayrollRunStatus.RELEASED, PayrollRunStatus.PAID]:
            raise ValidationError('Cannot recalculate a FINALIZED payroll run. Finalized payroll records are immutable.')

        if existing_run and existing_run.editing_deadline and timezone.now() > existing_run.editing_deadline:
            raise ValidationError(f'The editing window for this payroll run closed on {existing_run.editing_deadline}.')

        # Resolve effective schedule configuration for this batch scope
        batch_schedule = PayrollScheduleService.resolve_schedule(centre=centre, business=business)
        eff_cfg = batch_schedule['effective_config']
        eff_cfg_clean = {
            k: (str(v) if isinstance(v, (date, timezone.datetime, datetime.date if 'datetime' in locals() else date)) else v)
            for k, v in eff_cfg.items()
        }
        from datetime import date as d_cls, datetime as dt_cls
        for k, v in eff_cfg.items():
            if isinstance(v, (d_cls, dt_cls)):
                eff_cfg_clean[k] = str(v)
            else:
                eff_cfg_clean[k] = v

        expected_pay_date = PayrollScheduleService.calculate_expected_payment_date(period_end, eff_cfg)
        gen_type = eff_cfg.get('generation_type', GenerationType.MONTHLY)
        vis_policy = eff_cfg.get('visibility_policy', SalarySlipVisibilityPolicy.ON_FINALIZATION)
        editing_deadline = PayrollScheduleService.calculate_editing_deadline(eff_cfg, timezone.now())

        # For DAILY generation, do not apply monthly or weekly editing windows unless explicitly configured
        if gen_type == GenerationType.DAILY and eff_cfg.get('editable_period_duration', 0) == 0:
            editing_deadline = timezone.now()

        with transaction.atomic():
            run, _ = PayrollRun.objects.get_or_create(
                business=business,
                centre=centre,
                period_start=period_start,
                period_end=period_end,
                defaults={
                    'status': PayrollRunStatus.CALCULATING,
                    'approved_by': approved_by,
                    'expected_payment_date': expected_pay_date,
                    'generation_type': gen_type,
                    'pay_frequency': eff_cfg.get('pay_frequency', 'MONTHLY_CALENDAR'),
                    'generation_mode': eff_cfg.get('generation_mode', 'MANUAL'),
                    'editing_deadline': editing_deadline,
                    'visibility_policy': vis_policy,
                    'schedule_config_snapshot': eff_cfg_clean,
                }
            )
            run.status = PayrollRunStatus.CALCULATING
            run.expected_payment_date = expected_pay_date
            run.generation_type = gen_type
            run.pay_frequency = eff_cfg.get('pay_frequency', 'MONTHLY_CALENDAR')
            run.generation_mode = eff_cfg.get('generation_mode', 'MANUAL')
            run.editing_deadline = editing_deadline
            run.visibility_policy = vis_policy
            run.schedule_config_snapshot = eff_cfg_clean
            run.save(update_fields=[
                'status', 'expected_payment_date', 'generation_type', 'pay_frequency',
                'generation_mode', 'editing_deadline', 'visibility_policy',
                'schedule_config_snapshot', 'updated_at'
            ])

            emp_qs = Employee.objects.filter(
                business=business,
                employment_status='ACTIVE'
            )
            if centre:
                emp_qs = emp_qs.filter(branch=centre)

            employees = list(emp_qs)
            total_gross = Decimal('0.00')
            total_deductions = Decimal('0.00')
            total_net = Decimal('0.00')

            for emp in employees:
                # Historical immutability: If employee's payroll for this period is already PAID, preserve it
                existing_emp_payroll = Payroll.objects.filter(
                    business=business,
                    employee=emp,
                    period_start=period_start,
                    period_end=period_end
                ).first()

                if existing_emp_payroll and existing_emp_payroll.status == PayrollStatus.PAID:
                    total_gross += existing_emp_payroll.gross_amount
                    total_deductions += existing_emp_payroll.total_deductions
                    total_net += existing_emp_payroll.net_amount
                    continue

                calc = cls.calculate_employee_payroll(emp, period_start, period_end)

                payroll, _ = Payroll.objects.update_or_create(
                    business=business,
                    employee=emp,
                    period_start=period_start,
                    period_end=period_end,
                    defaults={
                        'payroll_run': run,
                        'working_days': calc['working_days'],
                        'paid_days': calc['paid_days'],
                        'unpaid_days': calc['unpaid_days'],
                        'half_days': calc['half_days'],
                        'ot_hours': calc['ot_hours'],
                        'gross_amount': calc['gross_amount'],
                        'total_deductions': calc['total_deductions'],
                        'net_amount': calc['net_amount'],
                        'currency': calc['currency'],
                        'salary_snapshot': calc['salary_snapshot'],
                        'schedule_snapshot': calc['schedule_snapshot'],
                        'editing_deadline': editing_deadline,
                        'visibility_policy': vis_policy,
                        'compensation_type': calc.get('compensation_type', ''),
                        'salary_unit': calc.get('salary_unit', ''),
                        'base_salary_amount': calc.get('base_salary_amount', Decimal('0.00')),
                        'status': PayrollStatus.DRAFT,
                    }
                )

                # Sync exceptions for this payroll
                cls.sync_payroll_exceptions(payroll, run, calc.get('exceptions_data', []))

                # Materialize PayrollLineItem records
                payroll.line_items.all().delete()
                line_objs = [
                    PayrollLineItem(
                        payroll=payroll,
                        name=line['name'],
                        line_type=line['line_type'],
                        amount=line['amount'],
                        rate=line['rate'],
                        units=line['units'],
                        is_deduction=line['is_deduction'],
                        source_compensation_item=line['source_compensation_item']
                    )
                    for line in calc['line_items']
                ]
                PayrollLineItem.objects.bulk_create(line_objs)

                # Incorporate any pre-existing reviewed exceptions
                cls.recalculate_employee_payroll_record(payroll)

                # Mark one-time components as applied in this payroll to prevent duplicate inclusion
                for line in calc['line_items']:
                    src_item = line.get('source_compensation_item')
                    if src_item and getattr(src_item, 'is_one_time', False):
                        src_item.is_applied = True
                        src_item.applied_in_payroll = payroll
                        src_item.applied_at = timezone.now()
                        src_item.save(update_fields=['is_applied', 'applied_in_payroll', 'applied_at'])

                total_gross += payroll.gross_amount
                total_deductions += payroll.total_deductions
                total_net += payroll.net_amount

            run.total_employees = len(employees)
            run.total_gross = total_gross
            run.total_deductions = total_deductions
            run.total_net = total_net
            run.status = PayrollRunStatus.DRAFT
            run.save()

            return run

    @classmethod
    def finalize_payroll_run(cls, run: PayrollRun, user=None) -> PayrollRun:
        """
        Finalizes a payroll run:
        1. Transitions run to FINALIZED with timestamp.
        2. Updates employee Payroll records to FINALIZED.
        3. Locks attendance for the period strictly for employees in the run.
        4. Logs immutable audit trail.
        """
        from rest_framework.exceptions import ValidationError
        from apps.attendance.models import AttendanceDay
        from apps.core.services.audit_service import AuditService

        if run.status in [PayrollRunStatus.FINALIZED, PayrollRunStatus.RELEASED, PayrollRunStatus.PAID]:
            raise ValidationError({'detail': 'This payroll run is already finalized.'})

        with transaction.atomic():
            now = timezone.now()
            run.status = PayrollRunStatus.FINALIZED
            run.finalized_at = now
            run.save(update_fields=['status', 'finalized_at', 'updated_at'])

            vis_policy = run.visibility_policy or SalarySlipVisibilityPolicy.ON_FINALIZATION

            run.payrolls.update(
                status=PayrollStatus.FINALIZED,
                finalized_at=now,
                visibility_policy=vis_policy
            )

            # Strictly lock attendance for the payroll period and employees in this run
            emp_ids = list(run.payrolls.values_list('employee_id', flat=True))
            AttendanceDay.objects.filter(
                employee_id__in=emp_ids,
                attendance_date__gte=run.period_start,
                attendance_date__lte=run.period_end
            ).update(is_locked=True)

            AuditService.log(
                user_or_request=user,
                action='FINALIZE_PAYROLL_RUN',
                entity_type='PayrollRun',
                entity_id=str(run.id),
                business=run.business,
                reason=f'Finalized and locked payroll run for period {run.period_start} to {run.period_end}'
            )

        return run

    @classmethod
    def release_payroll_run(cls, run: PayrollRun, user=None) -> PayrollRun:
        """
        Releases salary slips for a finalized payroll run, making them visible to employees.
        """
        from rest_framework.exceptions import ValidationError
        from apps.core.services.audit_service import AuditService

        if run.status not in [PayrollRunStatus.FINALIZED, PayrollRunStatus.APPROVED]:
            raise ValidationError({'detail': 'Only finalized payroll runs can be released to employees.'})

        with transaction.atomic():
            now = timezone.now()
            run.status = PayrollRunStatus.RELEASED
            run.released_at = now
            run.save(update_fields=['status', 'released_at', 'updated_at'])

            run.payrolls.update(
                status=PayrollStatus.RELEASED,
                released_at=now
            )

            AuditService.log(
                user_or_request=user,
                action='RELEASE_PAYROLL_RUN',
                entity_type='PayrollRun',
                entity_id=str(run.id),
                business=run.business,
                reason=f'Released payslips for payroll run {run.period_start} to {run.period_end}'
            )

        return run

    @classmethod
    def add_or_update_adjustment(
        cls,
        payroll: Payroll,
        adjustment_type: str,
        name: str,
        amount: Decimal,
        reason: str,
        user=None,
        is_deduction: bool = False
    ) -> PayrollAdjustment:
        """
        Applies a run-specific payroll adjustment during the open editing window.
        Validates editing deadline on backend.
        Preserves permanent employee compensation configuration.
        """
        from rest_framework.exceptions import ValidationError
        from apps.payroll.models import PayrollAdjustment, PayrollLineItem, PayrollLineItemType
        from apps.core.services.audit_service import AuditService

        run = payroll.payroll_run
        if not run:
            raise ValidationError({'detail': 'Payroll record has no associated batch run.'})

        now = timezone.now()
        if run.status in [PayrollRunStatus.FINALIZED, PayrollRunStatus.RELEASED, PayrollRunStatus.PAID, PayrollRunStatus.CANCELLED]:
            raise ValidationError({'detail': f'Cannot adjust payroll in {run.get_status_display()} state.'})

        if run.editing_deadline and now > run.editing_deadline:
            raise ValidationError({'detail': f'The payroll editing window closed on {run.editing_deadline}. Ordinary edits are no longer accepted.'})

        if not reason or not str(reason).strip():
            raise ValidationError({'reason': 'A mandatory reason is required for payroll adjustments.'})

        amount = Decimal(str(amount))
        if amount < 0:
            raise ValidationError({'amount': 'Adjustment amount cannot be negative.'})

        with transaction.atomic():
            p_locked = Payroll.objects.select_for_update().get(id=payroll.id)
            run_locked = PayrollRun.objects.select_for_update().get(id=run.id)

            existing_adj = PayrollAdjustment.objects.filter(
                payroll=p_locked,
                name=name,
                adjustment_type=adjustment_type
            ).first()

            prev_amount = existing_adj.new_amount if existing_adj else Decimal('0.00')

            if existing_adj:
                existing_adj.previous_amount = prev_amount
                existing_adj.new_amount = amount
                existing_adj.is_deduction = is_deduction
                existing_adj.reason = str(reason).strip()
                existing_adj.adjusted_by = user if (user and user.is_authenticated) else None
                existing_adj.save()
                adj = existing_adj
            else:
                adj = PayrollAdjustment.objects.create(
                    business=p_locked.business,
                    payroll_run=run_locked,
                    payroll=p_locked,
                    employee=p_locked.employee,
                    adjustment_type=adjustment_type,
                    name=name,
                    previous_amount=prev_amount,
                    new_amount=amount,
                    is_deduction=is_deduction,
                    reason=str(reason).strip(),
                    adjusted_by=user if (user and user.is_authenticated) else None
                )

            # Update or create PayrollLineItem on payroll
            line_type_map = {
                'EARNING': PayrollLineItemType.EARNING,
                'BONUS': PayrollLineItemType.BONUS,
                'ALLOWANCE': PayrollLineItemType.ALLOWANCE,
                'DEDUCTION': PayrollLineItemType.DEDUCTION,
                'OVERTIME': PayrollLineItemType.OVERTIME,
                'OTHER': PayrollLineItemType.OTHER,
            }
            mapped_lt = line_type_map.get(adjustment_type, PayrollLineItemType.OTHER)

            line_item, created = PayrollLineItem.objects.get_or_create(
                payroll=p_locked,
                name=name,
                defaults={
                    'line_type': mapped_lt,
                    'amount': amount,
                    'rate': amount,
                    'units': Decimal('1.00'),
                    'is_deduction': is_deduction,
                }
            )
            if not created:
                line_item.amount = amount
                line_item.rate = amount
                line_item.is_deduction = is_deduction
                line_item.line_type = mapped_lt
                line_item.save()

            # Recalculate employee payroll totals
            total_earnings = sum(it.amount for it in p_locked.line_items.filter(is_deduction=False))
            total_ded = sum(it.amount for it in p_locked.line_items.filter(is_deduction=True))
            p_locked.gross_amount = total_earnings
            p_locked.total_deductions = total_ded
            p_locked.net_amount = max(Decimal('0.00'), total_earnings - total_ded)
            p_locked.save(update_fields=['gross_amount', 'total_deductions', 'net_amount', 'updated_at'])

            # Recalculate run totals
            all_payrolls = run_locked.payrolls.all()
            run_locked.total_gross = sum(p.gross_amount for p in all_payrolls)
            run_locked.total_deductions = sum(p.total_deductions for p in all_payrolls)
            run_locked.total_net = sum(p.net_amount for p in all_payrolls)
            run_locked.save(update_fields=['total_gross', 'total_deductions', 'total_net', 'updated_at'])

            AuditService.log(
                user_or_request=user,
                action='PAYROLL_ADJUSTMENT',
                entity_type='PayrollAdjustment',
                entity_id=str(adj.id),
                old_data={'amount': float(prev_amount)},
                new_data={'amount': float(amount), 'reason': reason, 'item': name},
                business=p_locked.business,
                reason=f"Adjusted {name} on payroll for {p_locked.employee.full_name}: {prev_amount} -> {amount}"
            )

        return adj

    @classmethod
    def sync_payroll_exceptions(
        cls,
        payroll: Payroll,
        run: PayrollRun,
        exceptions_data: List[Dict[str, Any]]
    ):
        """
        Synchronizes evaluated attendance exceptions with persistent PayrollException rows.
        Preserves existing manager decisions (APPROVED, WAIVED, REJECTED).
        Updates or creates unreviewed exceptions.
        """
        from apps.payroll.models import PayrollException, PayrollExceptionReviewStatus

        existing_by_key = {
            (e.attendance_date, e.exception_type): e
            for e in payroll.exceptions.all()
        }

        seen_keys = set()
        for data in exceptions_data:
            key = (data['attendance_date'], data['exception_type'])
            seen_keys.add(key)
            existing = existing_by_key.get(key)

            if existing:
                # If manager already reviewed, DO NOT overwrite their decision
                if existing.review_status in [
                    PayrollExceptionReviewStatus.APPROVED,
                    PayrollExceptionReviewStatus.WAIVED,
                    PayrollExceptionReviewStatus.REJECTED
                ]:
                    continue
                # Update unreviewed exception
                existing.scheduled_hours = data.get('scheduled_hours', Decimal('8.00'))
                existing.actual_hours = data.get('actual_hours')
                existing.exception_reason = data.get('exception_reason', '')
                existing.proposed_amount = data.get('proposed_amount', Decimal('0.00'))
                existing.is_deduction = data.get('is_deduction', True)
                existing.policy_applied = data.get('policy_applied', '')
                existing.attendance_status = data.get('attendance_status', 'ABSENT')
                existing.save()
            else:
                PayrollException.objects.create(
                    business=payroll.business,
                    payroll_run=run,
                    payroll=payroll,
                    employee=payroll.employee,
                    centre=payroll.employee.branch,
                    attendance_date=data['attendance_date'],
                    attendance_status=data.get('attendance_status', 'ABSENT'),
                    scheduled_hours=data.get('scheduled_hours', Decimal('8.00')),
                    actual_hours=data.get('actual_hours'),
                    exception_type=data['exception_type'],
                    exception_reason=data.get('exception_reason', ''),
                    proposed_amount=data.get('proposed_amount', Decimal('0.00')),
                    is_deduction=data.get('is_deduction', True),
                    policy_applied=data.get('policy_applied', ''),
                    review_status=data.get('review_status', PayrollExceptionReviewStatus.CALCULATED),
                    is_applied_to_payroll=data.get('is_applied_to_payroll', True)
                )

        # Clean up obsolete unreviewed exceptions
        for key, existing in existing_by_key.items():
            if key not in seen_keys and existing.review_status in [
                PayrollExceptionReviewStatus.CALCULATED,
                PayrollExceptionReviewStatus.AWAITING_REVIEW
            ]:
                existing.delete()

    @classmethod
    def recalculate_employee_payroll_record(cls, payroll: Payroll) -> Payroll:
        """
        Recalculates an employee's Payroll line items and totals based on current
        PayrollException review states and manual PayrollAdjustment records.
        """
        # 1. Remove attendance-derived line items
        attendance_prefixes = [
            'Absence Deduction',
            'Late Arrival Deduction',
            'Half-Day Deduction',
            'Unpaid Leave',
            'Holiday / Week-Off Work',
        ]
        for prefix in attendance_prefixes:
            payroll.line_items.filter(name__startswith=prefix).delete()

        # Check if attendance deductions should be applied to payroll
        # For DAILY_WAGE, HOURLY_WAGE, or sub-monthly pro-rated periods, base pay is already proportional to days worked,
        # so absence/half-day deductions must not be added to line items.
        skip_attendance_deductions = False
        if payroll.compensation_type in ['DAILY_WAGE', 'HOURLY_WAGE'] or payroll.salary_unit == 'DAILY':
            skip_attendance_deductions = True
        elif (payroll.period_end - payroll.period_start).days + 1 < 28 and payroll.compensation_type != 'FIXED_CONTRACT':
            skip_attendance_deductions = True

        # 2. Re-create attendance line items from active applied exceptions
        applied_exceptions = list(payroll.exceptions.filter(is_applied_to_payroll=True))

        if not skip_attendance_deductions:
            # Absence
            absences = [e for e in applied_exceptions if e.exception_type in [PayrollExceptionType.ABSENT, PayrollExceptionType.PENDING_LEAVE]]
            if absences:
                tot_amt = sum(e.decision_amount if e.decision_amount is not None else e.proposed_amount for e in absences)
                rate = absences[0].proposed_amount if len(absences) == 1 else (tot_amt / Decimal(str(len(absences)))).quantize(Decimal('0.01'))
                PayrollLineItem.objects.create(
                    payroll=payroll,
                    name=f"Absence Deduction ({len(absences)} days)",
                    line_type=PayrollLineItemType.DEDUCTION,
                    amount=tot_amt,
                    rate=rate,
                    units=Decimal(str(len(absences))),
                    is_deduction=True
                )

            # Late
            lates = [e for e in applied_exceptions if e.exception_type == PayrollExceptionType.LATE]
            if lates:
                tot_amt = sum(e.decision_amount if e.decision_amount is not None else e.proposed_amount for e in lates)
                rate = lates[0].proposed_amount if len(lates) == 1 else (tot_amt / Decimal(str(len(lates)))).quantize(Decimal('0.01'))
                PayrollLineItem.objects.create(
                    payroll=payroll,
                    name=f"Late Arrival Deduction ({len(lates)} days)",
                    line_type=PayrollLineItemType.DEDUCTION,
                    amount=tot_amt,
                    rate=rate,
                    units=Decimal(str(len(lates))),
                    is_deduction=True
                )

            # Half Day
            halfs = [e for e in applied_exceptions if e.exception_type == PayrollExceptionType.HALF_DAY]
            if halfs:
                tot_amt = sum(e.decision_amount if e.decision_amount is not None else e.proposed_amount for e in halfs)
                rate = halfs[0].proposed_amount if len(halfs) == 1 else (tot_amt / Decimal(str(len(halfs)))).quantize(Decimal('0.01'))
                PayrollLineItem.objects.create(
                    payroll=payroll,
                    name=f"Half-Day Deduction ({len(halfs)} days)",
                    line_type=PayrollLineItemType.DEDUCTION,
                    amount=tot_amt,
                    rate=rate,
                    units=Decimal(str(len(halfs))),
                    is_deduction=True
                )

            # Unpaid Leave
            unpaids = [e for e in applied_exceptions if e.exception_type == PayrollExceptionType.UNPAID_LEAVE]
            if unpaids:
                tot_amt = sum(e.decision_amount if e.decision_amount is not None else e.proposed_amount for e in unpaids)
                rate = unpaids[0].proposed_amount if len(unpaids) == 1 else (tot_amt / Decimal(str(len(unpaids)))).quantize(Decimal('0.01'))
                PayrollLineItem.objects.create(
                    payroll=payroll,
                    name=f"Unpaid Leave ({len(unpaids)} days)",
                    line_type=PayrollLineItemType.UNPAID_LEAVE,
                    amount=tot_amt,
                    rate=rate,
                    units=Decimal(str(len(unpaids))),
                    is_deduction=True
                )

        # Holiday / Week-off work
        hw_items = [e for e in applied_exceptions if e.exception_type in [PayrollExceptionType.HOLIDAY_WORK, PayrollExceptionType.WEEK_OFF_WORK]]
        if hw_items:
            tot_amt = sum(e.decision_amount if e.decision_amount is not None else e.proposed_amount for e in hw_items)
            rate = hw_items[0].proposed_amount if len(hw_items) == 1 else (tot_amt / Decimal(str(len(hw_items)))).quantize(Decimal('0.01'))
            PayrollLineItem.objects.create(
                payroll=payroll,
                name=f"Holiday / Week-Off Work ({len(hw_items)} days)",
                line_type=PayrollLineItemType.EARNING,
                amount=tot_amt,
                rate=rate,
                units=Decimal(str(len(hw_items))),
                is_deduction=False
            )

        # 3. Re-calculate totals
        all_lines = list(payroll.line_items.all())
        total_earnings = sum(it.amount for it in all_lines if not it.is_deduction)
        total_ded = sum(it.amount for it in all_lines if it.is_deduction)

        payroll.gross_amount = total_earnings
        payroll.total_deductions = total_ded
        payroll.net_amount = max(Decimal('0.00'), total_earnings - total_ded)
        payroll.save(update_fields=['gross_amount', 'total_deductions', 'net_amount', 'updated_at'])
        return payroll

    @classmethod
    def review_payroll_exception(
        cls,
        exception: PayrollException,
        action: str,
        amount: Optional[Decimal] = None,
        reason: str = "",
        user = None
    ) -> PayrollException:
        """
        Processes a manager review decision on an attendance-to-payroll exception.
        Validates open editing window before finalization.
        Updates review_status, decision_amount, and is_applied_to_payroll.
        Atomically recalculates the employee's Payroll record and PayrollRun totals.
        Logs immutable audit event.
        """
        from rest_framework.exceptions import ValidationError
        from apps.payroll.models import (
            PayrollExceptionReviewStatus, PayrollRunStatus
        )
        from apps.core.services.audit_service import AuditService

        run = exception.payroll_run
        if not run:
            raise ValidationError({'detail': 'Exception record has no associated payroll run.'})

        now = timezone.now()
        if run.status in [PayrollRunStatus.FINALIZED, PayrollRunStatus.RELEASED, PayrollRunStatus.PAID, PayrollRunStatus.CANCELLED]:
            raise ValidationError({'detail': f'Cannot modify exceptions in {run.get_status_display()} state.'})

        if run.editing_deadline and now > run.editing_deadline:
            raise ValidationError({'detail': f'The payroll editing window closed on {run.editing_deadline}. Edits are no longer permitted.'})

        if not str(reason).strip():
            raise ValidationError({'reason': 'A mandatory audit reason is required for exception decisions.'})

        with transaction.atomic():
            exc_locked = PayrollException.objects.select_for_update().get(id=exception.id)
            payroll_locked = Payroll.objects.select_for_update().get(id=exc_locked.payroll_id)
            run_locked = PayrollRun.objects.select_for_update().get(id=run.id)

            old_status = exc_locked.review_status
            old_amt = exc_locked.decision_amount if exc_locked.decision_amount is not None else exc_locked.proposed_amount

            if action == 'WAIVE':
                exc_locked.review_status = PayrollExceptionReviewStatus.WAIVED
                exc_locked.decision_amount = Decimal('0.00')
                exc_locked.is_applied_to_payroll = False
            elif action == 'APPROVE':
                exc_locked.review_status = PayrollExceptionReviewStatus.APPROVED
                exc_locked.decision_amount = exc_locked.proposed_amount if amount is None else Decimal(str(amount))
                exc_locked.is_applied_to_payroll = True
            elif action == 'REJECT':
                exc_locked.review_status = PayrollExceptionReviewStatus.REJECTED
                exc_locked.decision_amount = Decimal('0.00')
                exc_locked.is_applied_to_payroll = False
            elif action == 'ADJUST_AMOUNT':
                exc_locked.review_status = PayrollExceptionReviewStatus.APPROVED
                dec_amt = Decimal(str(amount))
                exc_locked.decision_amount = dec_amt
                exc_locked.is_applied_to_payroll = (dec_amt > Decimal('0.00'))

            exc_locked.decision_by = user if (user and user.is_authenticated) else None
            exc_locked.decision_at = now
            exc_locked.decision_reason = str(reason).strip()
            exc_locked.save()

            # Recalculate employee payroll record atomically
            cls.recalculate_employee_payroll_record(payroll_locked)

            # Re-sum the PayrollRun totals
            all_payrolls = run_locked.payrolls.all()
            run_locked.total_gross = sum(p.gross_amount for p in all_payrolls)
            run_locked.total_deductions = sum(p.total_deductions for p in all_payrolls)
            run_locked.total_net = sum(p.net_amount for p in all_payrolls)
            run_locked.save(update_fields=['total_gross', 'total_deductions', 'total_net', 'updated_at'])

            AuditService.log(
                user_or_request=user,
                action='REVIEW_PAYROLL_EXCEPTION',
                entity_type='PayrollException',
                entity_id=str(exc_locked.id),
                old_data={'status': old_status, 'amount': float(old_amt)},
                new_data={
                    'action': action,
                    'status': exc_locked.review_status,
                    'decision_amount': float(exc_locked.decision_amount or 0),
                    'reason': reason
                },
                business=exc_locked.business,
                reason=f"Exception decision '{action}' applied for {exc_locked.employee.full_name} ({exc_locked.attendance_date}): {reason}"
            )

            return exc_locked

