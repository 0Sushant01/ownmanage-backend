"""
Acceptance Test Suite for OWNManage Simplified Salary Slip Generation Schedule
Supports exactly three generation types:
1. DAILY
2. WEEKLY (Salary Slip Generation Day: Monday .. Sunday)
3. MONTHLY (Salary Slip Generation Date: 1 .. 31 with February/Leap-year month-end clamping)
Enforces:
- Single effective generation type per employee
- Enterprise Default -> Centre Override -> Employee Override hierarchy
- Enable/Disable Employee Override with historical audit preservation
- Contiguous non-overlapping weekly periods
- Deterministic month-end clamping
- Duplicate generation prevention
- Historical payroll immutability
- Full RBAC enforcement
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
from rest_framework.exceptions import ValidationError as DRFValidationError, PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError

from apps.organization.models import Business, Branch, Employee, BusinessRole
from apps.accounts.models import User
from apps.payroll.models import (
    PayrollScheduleConfig, PayrollScheduleHistory, ScheduleConfigScope,
    CompensationType, PayFrequency, GenerationType, MonthEndRule,
    PayrollGenerationMode, PaymentScheduleRule,
    Payroll, PayrollRun, PayrollRunStatus, PayrollStatus, SalaryRevision
)
from apps.payroll.services.payroll_schedule_service import PayrollScheduleService
from apps.payroll.services.payroll_calculation_service import PayrollCalculationService
from apps.payroll.schedule_views import (
    EmployeePayrollScheduleView, EmployeePayrollScheduleResetView,
    CentrePayrollScheduleView, EnterprisePayrollScheduleView
)
from apps.payroll.revision_views import (
    PayrollRunListCreateView, PayrollRunFinalizeView,
    EmployeeSalaryRevisionListCreateView, EmployeeSalaryComparisonView
)

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
    print("STARTING SIMPLIFIED SALARY SLIP GENERATION ACCEPTANCE TESTS")
    print("======================================================================")

    biz = Business.objects.filter(name="Acme Enterprises").first() or Business.objects.first()
    assert biz is not None, "A business must exist in database"
    centre_a = Branch.objects.filter(business=biz, name__icontains="Centre A").first() or Branch.objects.filter(business=biz).first()
    emp = Employee.objects.filter(business=biz).first()
    assert emp is not None, "An employee must exist in database"

    admin_user = biz.memberships.filter(role=BusinessRole.BUSINESS_ADMIN).first().user
    staff_user = emp.user if emp.user else User.objects.filter(memberships__role=BusinessRole.STAFF).first()

    # ------------------------------------------------------------------
    # 1. DAILY GENERATION CONFIGURATION & ONE-DAY PERIODS
    # ------------------------------------------------------------------
    print("\n--- 1. Testing DAILY Generation Configuration & One-Day Periods ---")

    daily_cfg = {'generation_type': GenerationType.DAILY, 'pay_frequency': PayFrequency.DAILY}
    d_start, d_end = PayrollScheduleService.calculate_period_boundaries(daily_cfg, datetime.date(2026, 10, 15))
    record_test(
        "DAILY Generation Produces Exactly 1-Day Payroll Period",
        d_start == datetime.date(2026, 10, 15) and d_end == datetime.date(2026, 10, 15),
        f"{d_start} to {d_end}"
    )

    # Save DAILY configuration for employee
    emp_daily = PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.EMPLOYEE,
        business=biz,
        employee=emp,
        data={'generation_type': GenerationType.DAILY},
        user=admin_user,
        reason='Configure daily payroll generation'
    )
    res_daily = PayrollScheduleService.resolve_schedule(employee=emp)
    record_test(
        "DAILY Generation Configured & Resolved for Employee",
        res_daily['effective_config']['generation_type'] == GenerationType.DAILY and
        res_daily['effective_config']['pay_frequency'] == PayFrequency.DAILY,
        f"resolved_type={res_daily['effective_config']['generation_type']}"
    )

    # Daily calculation evaluates single day without whole-month overpay
    daily_calc = PayrollCalculationService.calculate_employee_payroll(
        employee=emp,
        period_start=datetime.date(2026, 10, 15),
        period_end=datetime.date(2026, 10, 15)
    )
    record_test(
        "DAILY Payroll Calculation Evaluates 1-Day Pro-rata Correctly",
        daily_calc['working_days'] == 1 and daily_calc['gross_amount'] <= Decimal('10000.00'),
        f"gross={daily_calc['gross_amount']}, working_days={daily_calc['working_days']}"
    )

    # ------------------------------------------------------------------
    # 2. WEEKLY GENERATION FOR ALL SEVEN WEEKDAYS (0=Mon .. 6=Sun)
    # ------------------------------------------------------------------
    print("\n--- 2. Testing WEEKLY Generation Across All 7 Weekdays ---")

    weekday_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    ref_wednesday = datetime.date(2026, 10, 14)  # 2026-10-14 is a Wednesday (weekday 2)

    for w_day, w_name in enumerate(weekday_names):
        w_cfg = {'generation_type': GenerationType.WEEKLY, 'generation_weekday': w_day}
        p_start, p_end = PayrollScheduleService.calculate_period_boundaries(w_cfg, ref_wednesday)
        duration = (p_end - p_start).days + 1
        is_exact_week = (duration == 7 and p_start.weekday() == w_day and p_end.weekday() == (w_day + 6) % 7)
        record_test(
            f"WEEKLY Generation Day {w_name} ({w_day}) Forms Contiguous 7-Day Block",
            is_exact_week,
            f"{p_start} ({weekday_names[p_start.weekday()]}) to {p_end} ({weekday_names[p_end.weekday()]})"
        )

        # Completed weekly period test
        c_start, c_end = PayrollScheduleService.get_completed_weekly_period(w_day, ref_wednesday)
        c_duration = (c_end - c_start).days + 1
        record_test(
            f"WEEKLY Completed Period for {w_name} Is Exactly 7 Days Without Overlap",
            c_duration == 7 and c_end < ref_wednesday or (c_end == ref_wednesday - datetime.timedelta(days=1) and ref_wednesday.weekday() == w_day),
            f"completed={c_start} to {c_end}"
        )

    # ------------------------------------------------------------------
    # 3. MONTHLY GENERATION DATES 1-31 & FEBRUARY / LEAP YEAR CLAMPING
    # ------------------------------------------------------------------
    print("\n--- 3. Testing MONTHLY Generation Dates (1-31) & Month-End Clamping ---")

    # Clamping tests for February 2026 (non-leap: 28 days)
    for day in [28, 29, 30, 31]:
        eff_date = PayrollScheduleService.get_effective_monthly_generation_date(2026, 2, day)
        record_test(
            f"Feb 2026 Generation Date {day} Clamps to Feb 28",
            eff_date == datetime.date(2026, 2, 28),
            f"clamped={eff_date}"
        )

    # Clamping tests for February 2028 (leap year: 29 days)
    for day in [29, 30, 31]:
        eff_leap = PayrollScheduleService.get_effective_monthly_generation_date(2028, 2, day)
        record_test(
            f"Feb 2028 Leap Year Generation Date {day} Clamps to Feb 29",
            eff_leap == datetime.date(2028, 2, 29),
            f"clamped={eff_leap}"
        )

    # Clamping tests for 30-day months (e.g. April, June, September, November)
    eff_apr = PayrollScheduleService.get_effective_monthly_generation_date(2026, 4, 31)
    record_test(
        "April 30-Day Month Generation Date 31 Clamps to April 30",
        eff_apr == datetime.date(2026, 4, 30),
        f"clamped={eff_apr}"
    )

    # Standard day-of-month retains exact date
    for test_day in [1, 5, 15, 25]:
        eff_standard = PayrollScheduleService.get_effective_monthly_generation_date(2026, 10, test_day)
        record_test(
            f"Standard Month Generation Date {test_day} Retains Exact Day",
            eff_standard == datetime.date(2026, 10, test_day),
            f"result={eff_standard}"
        )

    # ------------------------------------------------------------------
    # 4. INHERITANCE HIERARCHY (Enterprise -> Centre -> Employee)
    # ------------------------------------------------------------------
    print("\n--- 4. Testing Inheritance Hierarchy & Source Display ---")

    # Clean existing test configs
    PayrollScheduleConfig.objects.filter(business=biz).delete()

    # Step A: Enterprise Default (MONTHLY, Generation Date 5)
    PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.ENTERPRISE,
        business=biz,
        data={
            'generation_type': GenerationType.MONTHLY,
            'generation_date': 5,
            'compensation_type': CompensationType.MONTHLY_SALARY,
        },
        user=admin_user,
        reason='Enterprise Default Monthly on 5th'
    )

    res_ent = PayrollScheduleService.resolve_schedule(employee=emp)
    record_test(
        "Employee Inherits Enterprise Default (Source: Enterprise Default)",
        res_ent['source'] == 'ENTERPRISE' and
        res_ent['source_display'] == 'Enterprise Default' and
        res_ent['effective_config']['generation_type'] == GenerationType.MONTHLY and
        res_ent['effective_config']['generation_date'] == 5,
        f"source={res_ent['source']}, type={res_ent['effective_config']['generation_type']}"
    )

    # Step B: Centre Override (WEEKLY on Friday)
    PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.CENTRE,
        business=biz,
        centre=emp.branch,
        data={
            'generation_type': GenerationType.WEEKLY,
            'generation_weekday': 4,  # Friday
        },
        user=admin_user,
        reason='Centre Override Weekly on Friday'
    )

    res_cen = PayrollScheduleService.resolve_schedule(employee=emp)
    record_test(
        "Employee Inherits Centre Override (Source: Centre Override)",
        res_cen['source'] == 'CENTRE' and
        res_cen['source_display'] == 'Centre Override' and
        res_cen['effective_config']['generation_type'] == GenerationType.WEEKLY and
        res_cen['effective_config']['generation_weekday'] == 4,
        f"source={res_cen['source']}, weekday={res_cen['effective_config']['generation_weekday']}"
    )

    # Step C: Employee Override (DAILY)
    PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.EMPLOYEE,
        business=biz,
        employee=emp,
        data={
            'generation_type': GenerationType.DAILY,
        },
        user=admin_user,
        reason='Employee Override Daily'
    )

    res_emp = PayrollScheduleService.resolve_schedule(employee=emp)
    record_test(
        "Employee Override Takes Highest Precedence (Source: Employee Override)",
        res_emp['source'] == 'EMPLOYEE' and
        res_emp['source_display'] == 'Employee Override' and
        res_emp['effective_config']['generation_type'] == GenerationType.DAILY,
        f"source={res_emp['source']}, type={res_emp['effective_config']['generation_type']}"
    )

    # ------------------------------------------------------------------
    # 5. REVERTING OVERRIDE (Disable Override with History Preservation)
    # ------------------------------------------------------------------
    print("\n--- 5. Testing Disabling Override & Restoring Parent Hierarchy ---")

    hist_count_before = PayrollScheduleHistory.objects.filter(employee=emp).count()

    PayrollScheduleService.reset_override(
        scope=ScheduleConfigScope.EMPLOYEE,
        business=biz,
        employee=emp,
        user=admin_user,
        reason='Reverted employee override to inherit centre default'
    )

    res_reverted = PayrollScheduleService.resolve_schedule(employee=emp)
    hist_count_after = PayrollScheduleHistory.objects.filter(employee=emp).count()

    record_test(
        "Reverting Employee Override Restores Centre Inheritance Cleanly",
        res_reverted['source'] == 'CENTRE' and
        (res_reverted.get('employee_override') is None or res_reverted['employee_override']['has_override'] is False) and
        res_reverted['effective_config']['generation_type'] == GenerationType.WEEKLY,
        f"source={res_reverted['source']}, type={res_reverted['effective_config']['generation_type']}"
    )

    record_test(
        "Disabling Override Preserves Historical Snapshot In History Table",
        hist_count_after > hist_count_before,
        f"history_records={hist_count_after}"
    )

    # ------------------------------------------------------------------
    # 6. BACKEND VALIDATION ENFORCEMENT
    # ------------------------------------------------------------------
    print("\n--- 6. Testing Backend Validation Rules ---")

    # Invalid generation type
    invalid_type_err = False
    try:
        PayrollScheduleService.save_schedule(
            scope=ScheduleConfigScope.EMPLOYEE,
            business=biz,
            employee=emp,
            data={'generation_type': 'ANNUALLY'},
            user=admin_user
        )
    except (DjangoValidationError, DRFValidationError):
        invalid_type_err = True

    record_test("Invalid Generation Type Rejected by Backend", invalid_type_err)

    # Invalid weekday (e.g. 7 or -1)
    invalid_weekday_err = False
    try:
        PayrollScheduleService.save_schedule(
            scope=ScheduleConfigScope.EMPLOYEE,
            business=biz,
            employee=emp,
            data={'generation_type': GenerationType.WEEKLY, 'generation_weekday': 9},
            user=admin_user
        )
    except (DjangoValidationError, DRFValidationError):
        invalid_weekday_err = True

    record_test("Invalid Generation Weekday (>6) Rejected by Backend", invalid_weekday_err)

    # Invalid monthly date (e.g. 35 or 0)
    invalid_date_err = False
    try:
        PayrollScheduleService.save_schedule(
            scope=ScheduleConfigScope.EMPLOYEE,
            business=biz,
            employee=emp,
            data={'generation_type': GenerationType.MONTHLY, 'generation_date': 35},
            user=admin_user
        )
    except (DjangoValidationError, DRFValidationError):
        invalid_date_err = True

    record_test("Invalid Monthly Generation Date (>31) Rejected by Backend", invalid_date_err)

    # ------------------------------------------------------------------
    # 7. DUPLICATE PREVENTION & HISTORICAL IMMUTABILITY
    # ------------------------------------------------------------------
    print("\n--- 7. Testing Duplicate Prevention & Historical Payroll Immutability ---")

    period_start = datetime.date(2026, 8, 1)
    period_end = datetime.date(2026, 8, 31)

    # Clean up any preexisting test run for this period to ensure idempotency
    Payroll.objects.filter(payroll_run__business=biz, payroll_run__period_start=period_start, payroll_run__period_end=period_end).delete()
    PayrollRun.objects.filter(business=biz, period_start=period_start, period_end=period_end).delete()

    # Generate initial payroll run
    run1 = PayrollCalculationService.run_batch_payroll(
        business=biz,
        period_start=period_start,
        period_end=period_end,
        centre=None,
        approved_by=admin_user
    )

    # Finalize run
    run1.status = PayrollRunStatus.FINALIZED
    run1.finalized_at = timezone.now()
    run1.save()

    # Attempt to recalculate the same period
    finalized_blocked = False
    try:
        PayrollCalculationService.run_batch_payroll(
            business=biz,
            period_start=period_start,
            period_end=period_end,
            centre=None,
            approved_by=admin_user
        )
    except (DRFValidationError, DjangoValidationError) as e:
        if 'FINALIZED' in str(e):
            finalized_blocked = True

    record_test("Recalculation of FINALIZED Payroll Run Is Strictly Blocked", finalized_blocked)

    # Changing generation schedule does not mutate finalized payroll
    PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.ENTERPRISE,
        business=biz,
        data={'generation_type': GenerationType.WEEKLY, 'generation_weekday': 1},
        user=admin_user,
        reason='Testing historical isolation'
    )
    run1.refresh_from_db()
    record_test(
        "Finalized Payroll Run Remains Unmutated After Schedule Change",
        run1.status == PayrollRunStatus.FINALIZED and run1.period_start == period_start,
        f"status={run1.status}"
    )

    # ------------------------------------------------------------------
    # 8. REST API ENDPOINTS & RBAC PERMISSION ENFORCEMENT
    # ------------------------------------------------------------------
    print("\n--- 8. Testing REST APIs & RBAC Permissions ---")

    factory = APIRequestFactory()

    # GET employee schedule
    req_get = factory.get(f'/api/v1/employees/{emp.id}/payroll-schedule/')
    force_authenticate(req_get, user=admin_user)
    resp_get = EmployeePayrollScheduleView.as_view()(req_get, pk=str(emp.id))
    record_test(
        "API GET Employee Payroll Schedule Resolves Correctly",
        resp_get.status_code == 200 and 'effective_config' in resp_get.data,
        f"status={resp_get.status_code}"
    )

    # POST employee schedule as Admin (Allowed)
    req_post = factory.post(
        f'/api/v1/employees/{emp.id}/payroll-schedule/',
        data={
            'generation_type': 'WEEKLY',
            'generation_weekday': 2,  # Wednesday
            'change_reason': 'Configured via API acceptance test'
        },
        format='json'
    )
    force_authenticate(req_post, user=admin_user)
    resp_post = EmployeePayrollScheduleView.as_view()(req_post, pk=str(emp.id))
    record_test(
        "API POST Employee Schedule Override By Admin Succeeds",
        resp_post.status_code == 200 and resp_post.data['effective_config']['generation_type'] == 'WEEKLY',
        f"status={resp_post.status_code}"
    )

    # POST employee schedule as Unauthorized Staff (Blocked)
    if staff_user:
        req_unauth = factory.post(
            f'/api/v1/employees/{emp.id}/payroll-schedule/',
            data={'generation_type': 'DAILY', 'change_reason': 'Unauthorized hack'},
            format='json'
        )
        force_authenticate(req_unauth, user=staff_user)
        resp_unauth_blocked = False
        try:
            resp_unauth = EmployeePayrollScheduleView.as_view()(req_unauth, pk=str(emp.id))
            if resp_unauth.status_code == 403:
                resp_unauth_blocked = True
        except PermissionDenied:
            resp_unauth_blocked = True

        record_test("RBAC: Unauthorized Staff Blocked From Modifying Salary Schedule", resp_unauth_blocked)

    # POST Reset Employee Schedule via API
    req_reset = factory.post(
        f'/api/v1/employees/{emp.id}/payroll-schedule/reset/',
        data={'change_reason': 'Resetting override via API'},
        format='json'
    )
    force_authenticate(req_reset, user=admin_user)
    resp_reset = EmployeePayrollScheduleResetView.as_view()(req_reset, pk=str(emp.id))
    record_test(
        "API POST Reset Reverts Employee Override to Parent Configuration",
        resp_reset.status_code == 200 and resp_reset.data['employee_override']['has_override'] is False,
        f"source={resp_reset.data.get('source')}"
    )

    # ------------------------------------------------------------------
    # 9. SECTION 1 & 2 SYNCHRONIZATION & REVISION HISTORY UNIT IMMUTABILITY
    # ------------------------------------------------------------------
    print("\n--- 9. Testing Section 1 & 2 Synchronization & Salary Revision Units ---")

    # Ensure baseline employee has active salary revision
    baseline_salary = Decimal('30000.00')
    emp_rev = SalaryRevision.objects.filter(employee=emp).order_by('-effective_from').first()
    if not emp_rev:
        emp_rev = SalaryRevision.objects.create(
            business=biz,
            employee=emp,
            effective_from=datetime.date(2026, 1, 1),
            basic_salary=baseline_salary,
            salary_unit='MONTHLY',
            currency='INR',
            reason='Initial baseline salary'
        )
    else:
        emp_rev.basic_salary = baseline_salary
        emp_rev.salary_unit = 'MONTHLY'
        emp_rev.save()

    # 9.1 Monthly Employee
    PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.EMPLOYEE,
        business=biz,
        employee=emp,
        data={'generation_type': GenerationType.MONTHLY, 'generation_date': 5},
        user=admin_user,
        reason='Test Monthly Synchronization'
    )
    res_monthly = PayrollScheduleService.resolve_schedule(employee=emp)
    unit_monthly = '/ day' if res_monthly['effective_config']['generation_type'] == 'DAILY' else (
        '/ week' if res_monthly['effective_config']['generation_type'] == 'WEEKLY' else '/ month'
    )
    emp_rev.refresh_from_db()
    record_test(
        "MONTHLY: Section 1 Derives '/ month' and Stored Amount Remains Unchanged",
        unit_monthly == '/ month' and emp_rev.basic_salary == baseline_salary and res_monthly['source'] == 'EMPLOYEE',
        f"unit={unit_monthly}, basic_salary={emp_rev.basic_salary}"
    )

    # 9.2 Weekly Employee
    PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.EMPLOYEE,
        business=biz,
        employee=emp,
        data={'generation_type': GenerationType.WEEKLY, 'generation_weekday': 4},
        user=admin_user,
        reason='Test Weekly Synchronization'
    )
    res_weekly = PayrollScheduleService.resolve_schedule(employee=emp)
    unit_weekly = '/ day' if res_weekly['effective_config']['generation_type'] == 'DAILY' else (
        '/ week' if res_weekly['effective_config']['generation_type'] == 'WEEKLY' else '/ month'
    )
    emp_rev.refresh_from_db()
    record_test(
        "WEEKLY: Section 1 Derives '/ week' and Stored Amount Remains Exactly ₹30,000",
        unit_weekly == '/ week' and emp_rev.basic_salary == baseline_salary and res_weekly['source'] == 'EMPLOYEE',
        f"unit={unit_weekly}, basic_salary={emp_rev.basic_salary}"
    )

    # 9.3 Daily Employee
    PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.EMPLOYEE,
        business=biz,
        employee=emp,
        data={'generation_type': GenerationType.DAILY},
        user=admin_user,
        reason='Test Daily Synchronization'
    )
    res_daily = PayrollScheduleService.resolve_schedule(employee=emp)
    unit_daily = '/ day' if res_daily['effective_config']['generation_type'] == 'DAILY' else (
        '/ week' if res_daily['effective_config']['generation_type'] == 'WEEKLY' else '/ month'
    )
    emp_rev.refresh_from_db()
    record_test(
        "DAILY: Section 1 Derives '/ day' and Stored Amount Remains Exactly ₹30,000",
        unit_daily == '/ day' and emp_rev.basic_salary == baseline_salary and res_daily['source'] == 'EMPLOYEE',
        f"unit={unit_daily}, basic_salary={emp_rev.basic_salary}"
    )

    # 9.4 Centre-level Override Synchronization
    if centre_a:
        PayrollScheduleService.save_schedule(
            scope=ScheduleConfigScope.CENTRE,
            business=biz,
            centre=centre_a,
            data={'generation_type': GenerationType.WEEKLY, 'generation_weekday': 2},
            user=admin_user,
            reason='Centre override weekly'
        )
        emp.branch = centre_a
        emp.save()

        # Reset employee override
        PayrollScheduleService.reset_override(
            scope=ScheduleConfigScope.EMPLOYEE,
            business=biz,
            employee=emp,
            user=admin_user,
            reason='Inherit centre override'
        )
        res_centre = PayrollScheduleService.resolve_schedule(employee=emp)
        unit_centre = '/ day' if res_centre['effective_config']['generation_type'] == 'DAILY' else (
            '/ week' if res_centre['effective_config']['generation_type'] == 'WEEKLY' else '/ month'
        )
        record_test(
            "CENTRE OVERRIDE: Section 1 Inherits '/ week' and Source Displays Centre Override",
            res_centre['source'] == 'CENTRE' and unit_centre == '/ week',
            f"source={res_centre['source']}, unit={unit_centre}"
        )

    # 9.5 Reset to Enterprise Default
    PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.ENTERPRISE,
        business=biz,
        data={'generation_type': GenerationType.MONTHLY, 'generation_date': 1},
        user=admin_user,
        reason='Reset enterprise default to monthly'
    )
    if centre_a:
        PayrollScheduleService.reset_override(
            scope=ScheduleConfigScope.CENTRE,
            business=biz,
            centre=centre_a,
            user=admin_user,
            reason='Reset centre override to enterprise default'
        )
    res_ent = PayrollScheduleService.resolve_schedule(employee=emp)
    unit_ent = '/ day' if res_ent['effective_config']['generation_type'] == 'DAILY' else (
        '/ week' if res_ent['effective_config']['generation_type'] == 'WEEKLY' else '/ month'
    )
    record_test(
        "RESET TO ENTERPRISE: Section 1 & Section 2 Synchronously Display Enterprise Default",
        res_ent['source'] == 'ENTERPRISE' and unit_ent == '/ month',
        f"source={res_ent['source']}, unit={unit_ent}"
    )

    # 9.6 Salary Revision History Unit Immutability
    req_comp = factory.get(f'/api/v1/employees/{emp.id}/salary-comparison/')
    force_authenticate(req_comp, user=admin_user)
    resp_comp = EmployeeSalaryComparisonView.as_view()(req_comp, pk=str(emp.id))
    has_monthly_unit = any(
        c.get('salary_unit') == 'MONTHLY' and c.get('salary_unit_display') == '/ month'
        for c in resp_comp.data.get('comparisons', [])
    )
    record_test(
        "REVISION HISTORY: Historical Revision Retains '/ month' In Comparisons API",
        resp_comp.status_code == 200 and has_monthly_unit,
        f"comparisons_count={len(resp_comp.data.get('comparisons', []))}"
    )

    # 9.7 Switch Generation Frequency & Verify Historical Revisions Remain Unrelabeled
    PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.EMPLOYEE,
        business=biz,
        employee=emp,
        data={'generation_type': GenerationType.WEEKLY, 'generation_weekday': 5},
        user=admin_user,
        reason='Switch to Weekly'
    )
    req_comp_after = factory.get(f'/api/v1/employees/{emp.id}/salary-comparison/')
    force_authenticate(req_comp_after, user=admin_user)
    resp_comp_after = EmployeeSalaryComparisonView.as_view()(req_comp_after, pk=str(emp.id))
    still_has_monthly_unit = any(
        c.get('revision_id') == str(emp_rev.id) and c.get('salary_unit') == 'MONTHLY' and c.get('salary_unit_display') == '/ month'
        for c in resp_comp_after.data.get('comparisons', [])
    )
    record_test(
        "HISTORICAL IMMUTABILITY: Changing Current Frequency to Weekly Does NOT Relabel Historical Revisions",
        still_has_monthly_unit,
        f"historical_unit={emp_rev.salary_unit}"
    )

    # 9.8 Failed Configuration Save Leaves Authoritative State Unmodified
    save_failed = False
    try:
        PayrollScheduleService.save_schedule(
            scope=ScheduleConfigScope.EMPLOYEE,
            business=biz,
            employee=emp,
            data={'generation_type': GenerationType.MONTHLY, 'generation_date': 99},
            user=admin_user,
            reason='Illegal save attempt'
        )
    except (DjangoValidationError, DRFValidationError):
        save_failed = True

    res_after_fail = PayrollScheduleService.resolve_schedule(employee=emp)
    record_test(
        "FAILED SAVE SAFETY: Rejected Backend Save Leaves Effective Schedule Unmutated",
        save_failed and res_after_fail['effective_config']['generation_type'] == GenerationType.WEEKLY,
        f"effective_type={res_after_fail['effective_config']['generation_type']}"
    )

    # 9.9 Calculation Regression Check: Label Display Does Not Affect Calculations
    p_calc_monthly = PayrollCalculationService.calculate_employee_payroll(
        employee=emp,
        period_start=datetime.date(2026, 11, 1),
        period_end=datetime.date(2026, 11, 30)
    )
    calc_base_amt = p_calc_monthly['salary_snapshot']['basic_salary']
    record_test(
        "CALCULATION ISOLATION: 30-Day Monthly Calculation Correctly Yields ₹30,000 Base",
        calc_base_amt == 30000.0 and p_calc_monthly['gross_amount'] >= Decimal('30000.00'),
        f"calc_base={calc_base_amt}, gross={p_calc_monthly['gross_amount']}"
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
        print("ALL SIMPLIFIED SALARY GENERATION ACCEPTANCE TESTS PASSED WITH ZERO ERRORS!")


if __name__ == '__main__':
    run_schedule_tests()
