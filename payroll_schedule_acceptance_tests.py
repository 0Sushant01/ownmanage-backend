"""
Acceptance Test Suite for OWNManage Payroll Schedule, Generation Rules,
Salary Periods, and Payment Configuration.
"""

import os
import sys
import datetime
from decimal import Decimal

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.exceptions import ValidationError

from apps.organization.models import Business, Branch, Employee, BusinessRole
from apps.accounts.models import User
from apps.payroll.models import (
    PayrollScheduleConfig, PayrollScheduleHistory, ScheduleConfigScope,
    CompensationType, PayFrequency, MonthEndRule,
    PayrollGenerationMode, PaymentScheduleRule,
    Payroll, PayrollRun, PayrollRunStatus, PayrollStatus, SalaryRevision
)
from apps.payroll.services.payroll_schedule_service import PayrollScheduleService
from apps.payroll.services.payroll_calculation_service import PayrollCalculationService
from apps.payroll.schedule_views import (
    EmployeePayrollScheduleView, EmployeePayrollScheduleResetView,
    CentrePayrollScheduleView, EnterprisePayrollScheduleView
)
from apps.payroll.revision_views import PayrollRunListCreateView, PayrollRunFinalizeView

passed_checks = []
failed_checks = []

def record_test(name: str, passed: bool, details: str = ""):
    if passed:
        print(f"  [PASS] {name} {f'({details})' if details else ''}")
        passed_checks.append(name)
    else:
        print(f"  [FAIL] {name} - {details}")
        failed_checks.append((name, details))


def run_schedule_tests():
    print("======================================================================")
    print("STARTING PAYROLL SCHEDULE & COMPENSATION CYCLE ACCEPTANCE TESTS")
    print("======================================================================")

    biz = Business.objects.filter(name="Acme Enterprises").first() or Business.objects.first()
    assert biz is not None, "A business must exist in database"
    centre_a = Branch.objects.filter(business=biz, name__icontains="Centre A").first() or Branch.objects.filter(business=biz).first()
    emp = Employee.objects.filter(business=biz).first()
    assert emp is not None, "An employee must exist in database"

    admin_user = biz.memberships.filter(role=BusinessRole.BUSINESS_ADMIN).first().user

    # ------------------------------------------------------------------
    # 1. PAY FREQUENCY & PERIOD BOUNDARIES GENERATION
    # ------------------------------------------------------------------
    print("\n--- 1. Testing Pay Frequencies & Deterministic Period Calculations ---")

    # Calendar Monthly
    cal_cfg = {'pay_frequency': PayFrequency.MONTHLY_CALENDAR}
    p_start, p_end = PayrollScheduleService.calculate_period_boundaries(cal_cfg, datetime.date(2026, 10, 15))
    record_test("Calendar Monthly Period (October)", p_start == datetime.date(2026, 10, 1) and p_end == datetime.date(2026, 10, 31), f"{p_start} to {p_end}")

    # Daily
    daily_cfg = {'pay_frequency': PayFrequency.DAILY}
    d_start, d_end = PayrollScheduleService.calculate_period_boundaries(daily_cfg, datetime.date(2026, 10, 15))
    record_test("Daily Period Covers Single Date", d_start == datetime.date(2026, 10, 15) and d_end == datetime.date(2026, 10, 15))

    # Weekly (Monday start)
    weekly_mon = {'pay_frequency': PayFrequency.WEEKLY, 'week_start_day': 0}
    # 2026-10-15 is Thursday -> Monday is 2026-10-12, Sunday is 2026-10-18
    w_start, w_end = PayrollScheduleService.calculate_period_boundaries(weekly_mon, datetime.date(2026, 10, 15))
    record_test("Weekly Period (Mon to Sun)", w_start == datetime.date(2026, 10, 12) and w_end == datetime.date(2026, 10, 18), f"{w_start} to {w_end}")

    # Fortnightly (Anchor Date)
    fort_cfg = {'pay_frequency': PayFrequency.FORTNIGHTLY, 'anchor_date': datetime.date(2026, 10, 1)}
    f_start, f_end = PayrollScheduleService.calculate_period_boundaries(fort_cfg, datetime.date(2026, 10, 16))
    record_test("Fortnightly 14-Day Consecutive Block", f_start == datetime.date(2026, 10, 15) and f_end == datetime.date(2026, 10, 28), f"{f_start} to {f_end}")

    # ------------------------------------------------------------------
    # 2. MONTH-END HANDLING & SHORT MONTH/LEAP YEAR TEST
    # ------------------------------------------------------------------
    print("\n--- 2. Testing Month-End Handling (Short Months & Leap Years) ---")

    # Custom Cycle starting on 5th: 5 Oct to 4 Nov
    cycle_5 = {'pay_frequency': PayFrequency.MONTHLY_CUSTOM, 'custom_cycle_start_day': 5}
    c_start, c_end = PayrollScheduleService.calculate_period_boundaries(cycle_5, datetime.date(2026, 10, 20))
    record_test("Custom Monthly Cycle (5th to 4th)", c_start == datetime.date(2026, 10, 5) and c_end == datetime.date(2026, 11, 4), f"{c_start} to {c_end}")

    # Custom Cycle starting on 31st crossing into February (28 days in 2026)
    cycle_31 = {'pay_frequency': PayFrequency.MONTHLY_CUSTOM, 'custom_cycle_start_day': 31, 'month_end_rule': MonthEndRule.CLAMP_TO_LAST_DAY}
    jan_start, jan_end = PayrollScheduleService.calculate_period_boundaries(cycle_31, datetime.date(2026, 1, 31))
    record_test("31st Cycle Clamps deterministically to Feb 27 in 28-day Feb", jan_start == datetime.date(2026, 1, 31) and jan_end == datetime.date(2026, 2, 27), f"{jan_start} to {jan_end}")

    # Next cycle starts on Feb 28 (min(31, 28)) and ends on Mar 30 (day before Mar 31)
    feb_start, feb_end = PayrollScheduleService.calculate_period_boundaries(cycle_31, datetime.date(2026, 2, 28))
    record_test("Contiguous Feb Cycle Covers Feb 28 to Mar 30 Without Gap", feb_start == datetime.date(2026, 2, 28) and feb_end == datetime.date(2026, 3, 30), f"{feb_start} to {feb_end}")

    # Leap Year (2028: Feb has 29 days)
    leap_start, leap_end = PayrollScheduleService.calculate_period_boundaries(cycle_31, datetime.date(2028, 1, 31))
    record_test("Leap Year 2028 Clamps 31st Cycle to Feb 28 (day before Feb 29)", leap_start == datetime.date(2028, 1, 31) and leap_end == datetime.date(2028, 2, 28), f"{leap_start} to {leap_end}")

    # ------------------------------------------------------------------
    # 3. PAYMENT TIMING RULES INDEPENDENT OF GENERATION
    # ------------------------------------------------------------------
    print("\n--- 3. Testing Payment Timing Calculation Rules ---")

    p_end_ref = datetime.date(2026, 10, 31)

    # Day of Following Month (e.g. 7th)
    pay_next_month = PayrollScheduleService.calculate_expected_payment_date(p_end_ref, {'payment_rule': PaymentScheduleRule.DAY_OF_FOLLOWING_MONTH, 'payment_day_of_month': 7})
    record_test("Payment Rule: 7th of Following Month", pay_next_month == datetime.date(2026, 11, 7), f"{pay_next_month}")

    # Days after period end (e.g. 5 days after)
    pay_offset = PayrollScheduleService.calculate_expected_payment_date(p_end_ref, {'payment_rule': PaymentScheduleRule.DAYS_AFTER_PERIOD_END, 'payment_offset_days': 5})
    record_test("Payment Rule: 5 Days After Period End", pay_offset == datetime.date(2026, 11, 5), f"{pay_offset}")

    # Specified weekday (e.g. Following Friday)
    # 2026-10-31 is Saturday. Following Friday is 2026-11-06.
    pay_weekday = PayrollScheduleService.calculate_expected_payment_date(p_end_ref, {'payment_rule': PaymentScheduleRule.SPECIFIED_WEEKDAY, 'payment_weekday': 4})
    record_test("Payment Rule: Following Friday", pay_weekday == datetime.date(2026, 11, 6), f"{pay_weekday}")

    # Same day as period end
    pay_same = PayrollScheduleService.calculate_expected_payment_date(p_end_ref, {'payment_rule': PaymentScheduleRule.SAME_DAY_AS_PERIOD_END})
    record_test("Payment Rule: Same Day as Period End", pay_same == p_end_ref)

    # ------------------------------------------------------------------
    # 4. HIERARCHY RESOLUTION (Enterprise -> Centre -> Employee)
    # ------------------------------------------------------------------
    print("\n--- 4. Testing Configuration Inheritance & Overrides Hierarchy ---")

    # Clean existing test configs
    PayrollScheduleConfig.objects.filter(business=biz).delete()

    # Step A: Enterprise Default (Monthly Calendar)
    ent_cfg = PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.ENTERPRISE,
        business=biz,
        data={
            'compensation_type': CompensationType.MONTHLY_SALARY,
            'pay_frequency': PayFrequency.MONTHLY_CALENDAR,
            'approval_required': True,
            'payment_day_of_month': 7,
        },
        user=admin_user,
        reason='Initial Enterprise Default Setup'
    )

    res_emp = PayrollScheduleService.resolve_schedule(employee=emp)
    record_test(
        "Employee Inherits Enterprise Default When No Overrides Exist",
        res_emp['source'] == 'ENTERPRISE' and res_emp['effective_config']['pay_frequency'] == PayFrequency.MONTHLY_CALENDAR,
        f"source={res_emp['source']}"
    )

    # Step B: Centre Override (Weekly)
    PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.CENTRE,
        business=biz,
        centre=centre_a,
        data={
            'pay_frequency': PayFrequency.WEEKLY,
            'week_start_day': 0,
        },
        user=admin_user,
        reason='Centre A Weekly Settlement Override'
    )

    emp.branch = centre_a
    emp.save(update_fields=['branch'])

    res_emp_centre = PayrollScheduleService.resolve_schedule(employee=emp)
    record_test(
        "Employee Inherits Centre Override When Centre Has Custom Schedule",
        res_emp_centre['source'] == 'CENTRE' and res_emp_centre['effective_config']['pay_frequency'] == PayFrequency.WEEKLY,
        f"source={res_emp_centre['source']}, freq={res_emp_centre['effective_config']['pay_frequency']}"
    )

    # Step C: Employee Specific Override (Daily Wage)
    PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.EMPLOYEE,
        business=biz,
        centre=centre_a,
        employee=emp,
        data={
            'compensation_type': CompensationType.DAILY_WAGE,
            'pay_frequency': PayFrequency.WEEKLY,
            'payment_rule': PaymentScheduleRule.SPECIFIED_WEEKDAY,
            'payment_weekday': 4,
        },
        user=admin_user,
        reason='Employee Specific Daily Wage Override'
    )

    res_emp_custom = PayrollScheduleService.resolve_schedule(employee=emp)
    record_test(
        "Employee Uses Custom Employee Override When Configured",
        res_emp_custom['source'] == 'EMPLOYEE' and res_emp_custom['effective_config']['compensation_type'] == CompensationType.DAILY_WAGE,
        f"source={res_emp_custom['source']}, comp_type={res_emp_custom['effective_config']['compensation_type']}"
    )

    # Step D: Reset Employee Override to inherit Centre Default
    PayrollScheduleService.reset_override(
        scope=ScheduleConfigScope.EMPLOYEE,
        business=biz,
        centre=centre_a,
        employee=emp,
        user=admin_user,
        reason='Reverted employee to centre defaults'
    )

    res_emp_reverted = PayrollScheduleService.resolve_schedule(employee=emp)
    record_test(
        "Resetting Employee Override Restores Centre Default Inheritance",
        res_emp_reverted['source'] == 'CENTRE' and res_emp_reverted['has_override'] is True and res_emp_reverted['effective_config']['pay_frequency'] == PayFrequency.WEEKLY,
        f"source={res_emp_reverted['source']}"
    )

    # Verify History was preserved
    hist_count = PayrollScheduleHistory.objects.filter(employee=emp).count()
    record_test(
        "Historical Config Snapshots Preserved In PayrollScheduleHistory",
        hist_count >= 1,
        f"history_count={hist_count}"
    )

    # ------------------------------------------------------------------
    # 5. COMPENSATION TYPES IN PAYROLL CALCULATION ENGINE
    # ------------------------------------------------------------------
    print("\n--- 5. Testing Compensation Types in Payroll Engine ---")

    # Ensure revision exists for basic_salary = 30000 (effective on or after latest revision)
    # Clear any previous revisions for this employee in the test month to ensure deterministic base rate
    SalaryRevision.objects.filter(employee=emp, effective_from__gte=datetime.date(2026, 10, 1)).delete()
    SalaryRevision.objects.create(
        business=biz,
        employee=emp,
        basic_salary=Decimal('30000.00'),
        hourly_rate=Decimal('150.00'),
        effective_from=datetime.date(2026, 10, 1),
        reason='Standard compensation benchmark'
    )

    # Test Monthly Salary Calculation
    PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.EMPLOYEE,
        business=biz,
        centre=centre_a,
        employee=emp,
        data={'compensation_type': CompensationType.MONTHLY_SALARY},
        user=admin_user
    )
    calc_monthly = PayrollCalculationService.calculate_employee_payroll(emp, datetime.date(2026, 10, 1), datetime.date(2026, 10, 31))
    record_test(
        "Monthly Salary Uses Full Monthly Base Amount",
        calc_monthly['gross_amount'] >= Decimal('30000.00'),
        f"gross={calc_monthly['gross_amount']}"
    )

    # Test Daily Wage Calculation: ₹1000/day * paid days
    PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.EMPLOYEE,
        business=biz,
        centre=centre_a,
        employee=emp,
        data={'compensation_type': CompensationType.DAILY_WAGE},
        user=admin_user
    )
    calc_daily = PayrollCalculationService.calculate_employee_payroll(emp, datetime.date(2026, 10, 1), datetime.date(2026, 10, 31))
    daily_line = next((l for l in calc_daily['line_items'] if 'Daily Wages' in l['name']), None)
    record_test(
        "Daily Wage Calculation Generates Itemized Payable Days Line Item",
        daily_line is not None and daily_line['rate'] == Decimal('1000.00'),
        f"line_name={daily_line['name'] if daily_line else None}, rate={daily_line['rate'] if daily_line else None}"
    )

    # ------------------------------------------------------------------
    # 6. HISTORICAL PAYROLL IMMUTABILITY PROTECTION
    # ------------------------------------------------------------------
    print("\n--- 6. Testing Finalized Payroll Protection & Immutability ---")

    test_p_start = datetime.date(2026, 9, 1)
    test_p_end = datetime.date(2026, 9, 30)

    # Clean up any existing September run from prior test runs so test is fully idempotent
    from apps.payroll.models import Payroll
    prior_runs = PayrollRun.objects.filter(business=biz, period_start=test_p_start, period_end=test_p_end)
    Payroll.objects.filter(payroll_run__in=prior_runs).delete()
    prior_runs.delete()

    # Run batch payroll for September
    sep_run = PayrollCalculationService.run_batch_payroll(
        business=biz,
        period_start=test_p_start,
        period_end=test_p_end,
        centre=centre_a,
        approved_by=admin_user
    )

    # Finalize September Run
    sep_run.status = PayrollRunStatus.APPROVED
    sep_run.save()
    sep_run.status = PayrollRunStatus.FINALIZED
    sep_run.finalized_at = timezone.now()
    sep_run.save()

    # Attempting to re-run or recalculate finalized run MUST raise ValidationError
    recalc_blocked = False
    try:
        PayrollCalculationService.run_batch_payroll(
            business=biz,
            period_start=test_p_start,
            period_end=test_p_end,
            centre=centre_a,
            approved_by=admin_user
        )
    except ValidationError:
        recalc_blocked = True

    record_test(
        "Recalculation of FINALIZED Payroll Run Is Strictly Blocked",
        recalc_blocked,
        "ValidationError raised on attempt to overwrite finalized run"
    )

    # ------------------------------------------------------------------
    # 7. AUTOMATIC DRAFT GENERATION SAFETY
    # ------------------------------------------------------------------
    print("\n--- 7. Testing Automatic Draft Generation Safety ---")

    # Set Enterprise schedule to AUTOMATIC_DRAFT_AFTER_PERIOD_END
    PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.ENTERPRISE,
        business=biz,
        data={
            'generation_mode': PayrollGenerationMode.AUTOMATIC_DRAFT_AFTER_PERIOD_END,
            'generation_delay_days': 1
        },
        user=admin_user
    )

    # Test draft generation for completed August period
    prior_aug = PayrollRun.objects.filter(business=biz, period_start=datetime.date(2026, 8, 1), period_end=datetime.date(2026, 8, 31))
    Payroll.objects.filter(payroll_run__in=prior_aug).delete()
    prior_aug.delete()

    aug_runs = PayrollScheduleService.process_scheduled_drafts(
        business=biz,
        as_of_date=datetime.date(2026, 9, 2)
    )

    for r in aug_runs:
        record_test(
            "Scheduled Generation Produces DRAFT Status Only (Never Finalized or Paid)",
            r.status == PayrollRunStatus.DRAFT,
            f"run_status={r.status}"
        )

    # ------------------------------------------------------------------
    # 8. REST API ENDPOINTS & RBAC ACCESS CONTROL
    # ------------------------------------------------------------------
    print("\n--- 8. Testing REST APIs & Granular RBAC Permissions ---")

    factory = APIRequestFactory()

    # GET /api/v1/employees/<id>/payroll-schedule/
    req_get = factory.get(f'/api/v1/employees/{emp.id}/payroll-schedule/')
    force_authenticate(req_get, user=admin_user)
    resp_get = EmployeePayrollScheduleView.as_view()(req_get, pk=str(emp.id))
    record_test(
        "API GET Employee Payroll Schedule Returns 200 with Source & Resolution",
        resp_get.status_code == 200 and 'effective_config' in resp_get.data and 'source' in resp_get.data,
        f"status={resp_get.status_code}, source={resp_get.data.get('source')}"
    )

    # POST /api/v1/employees/<id>/payroll-schedule/
    req_post = factory.post(
        f'/api/v1/employees/{emp.id}/payroll-schedule/',
        data={
            'compensation_type': 'DAILY_WAGE',
            'pay_frequency': 'WEEKLY',
            'change_reason': 'Updated via API acceptance test'
        },
        format='json'
    )
    force_authenticate(req_post, user=admin_user)
    resp_post = EmployeePayrollScheduleView.as_view()(req_post, pk=str(emp.id))
    record_test(
        "API POST Employee Payroll Schedule Saves Override Successfully",
        resp_post.status_code == 200 and resp_post.data['effective_config']['compensation_type'] == 'DAILY_WAGE',
        f"status={resp_post.status_code}"
    )

    # POST /api/v1/employees/<id>/payroll-schedule/reset/
    req_reset = factory.post(
        f'/api/v1/employees/{emp.id}/payroll-schedule/reset/',
        data={'change_reason': 'Resetting via API acceptance test'},
        format='json'
    )
    force_authenticate(req_reset, user=admin_user)
    resp_reset = EmployeePayrollScheduleResetView.as_view()(req_reset, pk=str(emp.id))
    record_test(
        "API POST Employee Schedule Reset Reverts to Parent Default",
        resp_reset.status_code == 200 and resp_reset.data['source'] in ['CENTRE', 'ENTERPRISE'],
        f"status={resp_reset.status_code}, source={resp_reset.data.get('source')}"
    )

    # ------------------------------------------------------------------
    # Summary Report
    # ------------------------------------------------------------------
    print("\n======================================================================")
    print(f"ACCEPTANCE TESTS FINISHED: {len(passed_checks)} PASSED, {len(failed_checks)} FAILED")
    print("======================================================================")
    if failed_checks:
        for f in failed_checks:
            print(f"FAILED: {f[0]} -> {f[1]}")
        sys.exit(1)
    else:
        print("ALL PAYROLL SCHEDULE ACCEPTANCE TESTS COMPLETED SUCCESSFULLY WITH ZERO ERRORS!")


if __name__ == '__main__':
    run_schedule_tests()
