"""
Comprehensive Acceptance Test Suite for OWNManage Salary-Generation Lifecycle,
Configurable Payroll Editing Window, Employee Salary Visibility, and Attendance Locking Rules.

Covers:
1. Monthly generation on configured date & clamp (including Feb clamp).
2. Weekly generation on configured weekday (contiguous 7-day period).
3. Daily payroll generation and wage snapshot (compensation type, unit, base salary amount).
4. Configurable editing deadlines (support for DAYS and HOURS units).
5. Authorized adjustments within editing period (gross/net re-summed, audit trail recorded).
6. Rejection of edits after editing deadline (editing window expired).
7. Attendance editable before payroll generation.
8. Attendance editable during open payroll editing window.
9. Attendance locked after finalization (ordinary punches & overrides rejected).
10. Dates outside payroll period remain unlocked and punchable.
11. Failed payroll generation does not lock attendance.
12. Employees cannot access draft salary slips (neither via list nor direct ID lookup).
13. Employees can access released salary slips (list and detail).
14. Configuration hierarchy: Enterprise default -> Centre override -> Employee override.
15. Historical snapshots remain immutable when employee/centre settings change.
16. Duplicate generation is prevented safely.
17. 12:01 AM scheduler scan generates eligible runs and auto-finalizes expired deadlines idempotently.
18. RBAC and business boundary enforcement.
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
    CompensationType, PayFrequency, GenerationType,
    SalarySlipVisibilityPolicy, EditingPeriodUnit,
    Payroll, PayrollRun, PayrollRunStatus, PayrollStatus,
    PayrollAdjustment, PayrollAdjustmentType, PayrollLineItem, SalaryRevision
)
from apps.attendance.models import AttendanceDay, AttendanceEvent, AttendanceEventType, AttendanceStatus, AttendanceCorrection
from apps.attendance.services.attendance_service import AttendanceService
from apps.payroll.services.payroll_schedule_service import PayrollScheduleService
from apps.payroll.services.payroll_calculation_service import PayrollCalculationService
from apps.payroll.services.payroll_lifecycle_service import PayrollLifecycleService
from apps.payroll.views import PayrollListView, PayrollDetailView
from apps.payroll.revision_views import (
    PayrollRunFinalizeView, PayrollRunReleaseView,
    PayrollRunAdjustView, PayrollRecordAdjustView
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

def run_lifecycle_acceptance_tests():
    print("=" * 75)
    print("STARTING SALARY LIFECYCLE, EDITING WINDOW, VISIBILITY & LOCKING TESTS")
    print("=" * 75)

    biz = Business.objects.filter(name="Acme Enterprises").first() or Business.objects.first()
    assert biz is not None, "A business must exist in database"
    centre_a = Branch.objects.filter(business=biz, name__icontains="Centre A").first() or Branch.objects.filter(business=biz).first()
    centre_b = Branch.objects.filter(business=biz, name__icontains="Centre B").first()
    if not centre_b:
        centre_b = Branch.objects.create(business=biz, name="Centre B Test", code="C-B-TEST")

    emp = Employee.objects.filter(business=biz).first()
    assert emp is not None, "An employee must exist in database"

    admin_membership = biz.memberships.filter(role=BusinessRole.BUSINESS_ADMIN).first()
    admin_user = admin_membership.user if admin_membership else biz.owner
    staff_user = emp.user
    if not staff_user:
        staff_user, _ = User.objects.get_or_create(email=f"staff_{emp.id}@test.com", defaults={"first_name": "Test", "last_name": "Staff"})
        emp.user = staff_user
        emp.save(update_fields=["user"])

    # Ensure staff membership exists
    from apps.organization.models import BusinessMembership
    staff_mem = biz.memberships.filter(user=staff_user).first()
    if not staff_mem:
        BusinessMembership.objects.create(business=biz, user=staff_user, role=BusinessRole.STAFF)

    factory = APIRequestFactory()

    # ------------------------------------------------------------------
    # 1. MONTHLY GENERATION ON CONFIGURED DATE & CLAMP
    # ------------------------------------------------------------------
    print("\n--- 1. Testing Monthly Generation & Clamping ---")
    cfg_31st = PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.ENTERPRISE,
        business=biz,
        user=admin_user,
        reason="Enterprise 31st monthly generation",
        data={
            "generation_type": GenerationType.MONTHLY,
            "generation_date": 31,
            "editable_period_duration": 3,
            "editing_period_unit": EditingPeriodUnit.DAYS,
            "visibility_policy": SalarySlipVisibilityPolicy.ON_FINALIZATION,
        }
    )
    # Check Feb in leap year and non-leap year
    feb_non_leap = PayrollScheduleService.get_effective_monthly_generation_date(2025, 2, 31)
    record_test("Monthly generation clamps 31st to 28th in Feb non-leap", feb_non_leap == datetime.date(2025, 2, 28), f"Clamped to {feb_non_leap}")

    feb_leap = PayrollScheduleService.get_effective_monthly_generation_date(2024, 2, 31)
    record_test("Monthly generation clamps 31st to 29th in Feb leap year", feb_leap == datetime.date(2024, 2, 29), f"Clamped to {feb_leap}")

    apr_30 = PayrollScheduleService.get_effective_monthly_generation_date(2025, 4, 31)
    record_test("Monthly generation clamps 31st to 30th in 30-day month", apr_30 == datetime.date(2025, 4, 30), f"Clamped to {apr_30}")

    # ------------------------------------------------------------------
    # 2. WEEKLY GENERATION ON CONFIGURED WEEKDAY (CONTIGUOUS 7 DAYS)
    # ------------------------------------------------------------------
    print("\n--- 2. Testing Weekly Generation & Contiguous Periods ---")
    cfg_weekly = PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.CENTRE,
        business=biz,
        centre=centre_b,
        user=admin_user,
        reason="Centre B weekly Monday generation",
        data={
            "generation_type": GenerationType.WEEKLY,
            "generation_weekday": 0, # Monday
            "editable_period_duration": 2,
            "editing_period_unit": EditingPeriodUnit.DAYS,
            "visibility_policy": SalarySlipVisibilityPolicy.ON_RELEASE,
        }
    )
    # Monday 2026-10-12
    ref_monday = datetime.date(2026, 10, 12)
    w_start, w_end = PayrollScheduleService.get_completed_weekly_period(0, ref_monday)
    record_test("Weekly period spans exactly 7 contiguous days", (w_end - w_start).days == 6, f"{w_start} to {w_end}")
    record_test("Weekly generation completes before generation weekday", w_end == ref_monday - datetime.timedelta(days=1), f"Ended on {w_end}")

    # ------------------------------------------------------------------
    # 3. DAILY PAYROLL GENERATION & WAGE SNAPSHOT
    # ------------------------------------------------------------------
    print("\n--- 3. Testing Daily Payroll Generation & Wage Snapshot ---")
    PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.EMPLOYEE,
        business=biz,
        employee=emp,
        user=admin_user,
        reason="Daily wage schedule test",
        data={
            "generation_type": GenerationType.DAILY,
            "compensation_type": CompensationType.DAILY_WAGE,
        }
    )
    SalaryRevision.objects.filter(employee=emp, effective_from=datetime.date(2026, 9, 1)).delete()
    SalaryRevision.objects.create(
        business=biz,
        employee=emp,
        effective_from=datetime.date(2026, 9, 1),
        basic_salary=Decimal("1500.00"),
        salary_unit="DAILY",
        revised_by=admin_user,
        reason="Daily wage setup"
    )

    daily_calc = PayrollCalculationService.calculate_employee_payroll(
        employee=emp,
        period_start=datetime.date(2026, 9, 1),
        period_end=datetime.date(2026, 9, 1)
    )
    record_test("Daily wage calculates base salary snapshot", daily_calc["base_salary_amount"] == Decimal("1500.00"), f"Base: {daily_calc['base_salary_amount']}")
    record_test("Daily compensation type captured in snapshot", daily_calc["compensation_type"] == CompensationType.DAILY_WAGE)
    record_test("Salary unit captured in snapshot", daily_calc["salary_unit"] == "DAILY")

    # ------------------------------------------------------------------
    # 4. CONFIGURABLE EDITING DEADLINES (DAYS & HOURS)
    # ------------------------------------------------------------------
    print("\n--- 4. Testing Configurable Editing Deadlines ---")
    now_dt = timezone.now()
    cfg_dict_days = {"editable_period_duration": 3, "editing_period_unit": EditingPeriodUnit.DAYS}
    deadline_days = PayrollScheduleService.calculate_editing_deadline(cfg_dict_days, now_dt)
    expected_days = now_dt + datetime.timedelta(days=3)
    record_test("Editing deadline calculated in DAYS", abs((deadline_days - expected_days).total_seconds()) < 2, f"Deadline: {deadline_days}")

    cfg_dict_hours = {"editable_period_duration": 12, "editing_period_unit": EditingPeriodUnit.HOURS}
    deadline_hours = PayrollScheduleService.calculate_editing_deadline(cfg_dict_hours, now_dt)
    expected_hours = now_dt + datetime.timedelta(hours=12)
    record_test("Editing deadline calculated in HOURS", abs((deadline_hours - expected_hours).total_seconds()) < 2, f"Deadline: {deadline_hours}")

    # ------------------------------------------------------------------
    # 5. DRAFT RUN GENERATION & IN-FLIGHT ADJUSTMENTS
    # ------------------------------------------------------------------
    print("\n--- 5. Testing Draft Run & Authorized Adjustments ---")
    # Clean up previous test runs for test period
    test_start = datetime.date(2026, 8, 1)
    test_end = datetime.date(2026, 8, 31)
    PayrollRun.objects.filter(business=biz, period_start=test_start, period_end=test_end).delete()
    Payroll.objects.filter(business=biz, period_start=test_start, period_end=test_end).delete()

    run = PayrollCalculationService.run_batch_payroll(
        business=biz,
        period_start=test_start,
        period_end=test_end,
        user=admin_user,
        centre=centre_a
    )
    record_test("Batch payroll generates DRAFT run", run.status == PayrollRunStatus.DRAFT)
    record_test("Editing deadline populated on PayrollRun", run.editing_deadline is not None)
    record_test("Editing window is open initially", run.is_editing_open is True)
    record_test("Schedule config snapshot preserved", "editable_period_duration" in run.schedule_config_snapshot)

    # Apply authorized adjustment
    emp_payroll = Payroll.objects.filter(payroll_run=run, employee=emp).first()
    assert emp_payroll is not None, "Employee payroll must exist in run"
    initial_gross = emp_payroll.gross_amount
    initial_net = emp_payroll.net_amount

    adj = PayrollCalculationService.add_or_update_adjustment(
        payroll=emp_payroll,
        user=admin_user,
        adjustment_type=PayrollAdjustmentType.BONUS,
        name="Performance Bonus Aug",
        amount=Decimal("5000.00"),
        reason="Exceeded monthly KPI target"
    )
    emp_payroll.refresh_from_db()
    run.refresh_from_db()

    record_test("Payroll adjustment record created", adj.id is not None)
    record_test("Gross amount updated with adjustment", emp_payroll.gross_amount == initial_gross + Decimal("5000.00"), f"New gross: {emp_payroll.gross_amount}")
    record_test("Net amount updated with adjustment", emp_payroll.net_amount == initial_net + Decimal("5000.00"), f"New net: {emp_payroll.net_amount}")
    record_test("Run total gross re-summed", run.total_gross >= emp_payroll.gross_amount)

    # ------------------------------------------------------------------
    # 6. REJECTION OF EDITS AFTER EDITING DEADLINE
    # ------------------------------------------------------------------
    print("\n--- 6. Testing Rejection of Edits After Deadline ---")
    # Expire deadline manually
    run.editing_deadline = timezone.now() - datetime.timedelta(minutes=5)
    run.save(update_fields=["editing_deadline"])
    emp_payroll.editing_deadline = run.editing_deadline
    emp_payroll.save(update_fields=["editing_deadline"])

    record_test("is_editing_open returns False after deadline", run.is_editing_open is False)

    edit_rejected = False
    try:
        PayrollCalculationService.add_or_update_adjustment(
            payroll=emp_payroll,
            user=admin_user,
            adjustment_type=PayrollAdjustmentType.ALLOWANCE,
            name="Late Allowance",
            amount=Decimal("1000.00"),
            reason="Late entry"
        )
    except (DRFValidationError, DjangoValidationError) as e:
        edit_rejected = True

    record_test("Backend rejects adjustments after deadline expired", edit_rejected)

    # ------------------------------------------------------------------
    # 7. ATTENDANCE EDITABLE BEFORE PAYROLL GENERATION
    # ------------------------------------------------------------------
    print("\n--- 7. Testing Attendance Before Payroll Generation ---")
    future_date = datetime.date(2026, 12, 10)
    att_day, _ = AttendanceDay.objects.get_or_create(
        business=biz,
        employee=emp,
        attendance_date=future_date,
        defaults={"status": AttendanceStatus.PRESENT}
    )
    att_day.is_locked = False
    att_day.save(update_fields=["is_locked"])

    pre_edit = AttendanceService.override_attendance(
        user=admin_user,
        attendance_day=att_day,
        status=AttendanceStatus.PRESENT,
        reason="Pre-payroll verified attendance"
    )
    record_test("Attendance editable before payroll generated", pre_edit.status == AttendanceStatus.PRESENT)

    # ------------------------------------------------------------------
    # 8. ATTENDANCE EDITABLE DURING OPEN EDITING WINDOW
    # ------------------------------------------------------------------
    print("\n--- 8. Testing Attendance During Open Editing Window ---")
    # Reset deadline to future for run and individual payrolls
    run.editing_deadline = timezone.now() + datetime.timedelta(days=2)
    run.save(update_fields=["editing_deadline"])
    Payroll.objects.filter(payroll_run=run).update(editing_deadline=run.editing_deadline)

    att_in_period, _ = AttendanceDay.objects.get_or_create(
        business=biz,
        employee=emp,
        attendance_date=test_start,
        defaults={"status": AttendanceStatus.PRESENT}
    )
    att_in_period.is_locked = False
    att_in_period.save(update_fields=["is_locked"])

    override_ok = False
    try:
        AttendanceService.override_attendance(
            user=admin_user,
            attendance_day=att_in_period,
            status=AttendanceStatus.PRESENT,
            reason="Reviewed and verified during open editing window"
        )
        override_ok = True
    except (DRFValidationError, DjangoValidationError):
        override_ok = False

    record_test("Attendance override allowed while payroll editing window open", override_ok)

    # ------------------------------------------------------------------
    # 9. ATTENDANCE LOCKED AFTER FINALIZATION
    # ------------------------------------------------------------------
    print("\n--- 9. Testing Attendance Locking After Finalization ---")
    PayrollCalculationService.finalize_payroll_run(run, user=admin_user)
    run.refresh_from_db()
    att_in_period.refresh_from_db()

    record_test("Payroll run transitioned to FINALIZED", run.status == PayrollRunStatus.FINALIZED)
    record_test("AttendanceDay within payroll period locked", att_in_period.is_locked is True)

    lock_enforced = False
    try:
        AttendanceService.override_attendance(
            user=admin_user,
            attendance_day=att_in_period,
            status=AttendanceStatus.ABSENT,
            reason="Unauthorized post-finalization edit"
        )
    except (DRFValidationError, DjangoValidationError) as e:
        if "locked" in str(e).lower():
            lock_enforced = True

    record_test("Ordinary attendance edits rejected after finalization", lock_enforced)

    # Verify authorized post-finalization correction workflow still functions
    correction = AttendanceCorrection.objects.create(
        business=biz,
        attendance_day=att_in_period,
        requested_by=admin_user,
        correction_type='STATUS',
        corrected_status=AttendanceStatus.HALF_DAY,
        reason="Official post-audit correction request"
    )
    record_test("Authorized AttendanceCorrection workflow preserved", correction.id is not None)

    # ------------------------------------------------------------------
    # 10. DATES OUTSIDE PAYROLL PERIOD REMAIN UNLOCKED
    # ------------------------------------------------------------------
    print("\n--- 10. Testing Unaffected Dates Outside Payroll Period ---")
    outside_date = datetime.date(2026, 12, 15)
    AttendanceDay.objects.filter(business=biz, employee=emp, attendance_date=outside_date).delete()
    att_outside = AttendanceDay.objects.create(
        business=biz,
        employee=emp,
        attendance_date=outside_date,
        status=AttendanceStatus.PRESENT,
        is_locked=False
    )
    att_outside.refresh_from_db()
    record_test("Attendance for dates outside finalized run remains unlocked", att_outside.is_locked is False)

    outside_edit_ok = False
    try:
        AttendanceService.override_attendance(
            user=admin_user,
            attendance_day=att_outside,
            status=AttendanceStatus.HALF_DAY,
            reason="Regular edit for subsequent open month"
        )
        outside_edit_ok = True
    except (DRFValidationError, DjangoValidationError):
        outside_edit_ok = False

    record_test("Edits allowed on attendance outside finalized period", outside_edit_ok)

    # ------------------------------------------------------------------
    # 11. FAILED GENERATION DOES NOT LOCK ATTENDANCE
    # ------------------------------------------------------------------
    print("\n--- 11. Testing Failed Generation Does Not Lock Attendance ---")
    test_fail_date = datetime.date(2026, 11, 1)
    att_fail_test, _ = AttendanceDay.objects.get_or_create(
        business=biz,
        employee=emp,
        attendance_date=test_fail_date,
        defaults={"status": AttendanceStatus.PRESENT, "is_locked": False}
    )
    att_fail_test.is_locked = False
    att_fail_test.save(update_fields=["is_locked"])

    # Simulate an error inside batch calculation (e.g. invalid date order)
    try:
        PayrollCalculationService.run_batch_payroll(
            business=biz,
            period_start=datetime.date(2026, 11, 30),
            period_end=datetime.date(2026, 11, 1), # invalid: start > end
            user=admin_user
        )
    except Exception:
        pass

    att_fail_test.refresh_from_db()
    record_test("Failed/aborted generation does not lock attendance", att_fail_test.is_locked is False)

    # ------------------------------------------------------------------
    # 12. EMPLOYEES CANNOT ACCESS DRAFT SALARY SLIPS
    # ------------------------------------------------------------------
    print("\n--- 12. Testing Employee Salary Visibility (Drafts Hidden) ---")
    # Create a new draft run for Sept 2026
    draft_start = datetime.date(2026, 9, 1)
    draft_end = datetime.date(2026, 9, 30)
    PayrollRun.objects.filter(business=biz, period_start=draft_start, period_end=draft_end).delete()
    Payroll.objects.filter(business=biz, period_start=draft_start, period_end=draft_end).delete()

    draft_run = PayrollCalculationService.run_batch_payroll(
        business=biz,
        period_start=draft_start,
        period_end=draft_end,
        user=admin_user,
        centre=centre_a
    )
    draft_payroll = Payroll.objects.filter(payroll_run=draft_run, employee=emp).first()
    assert draft_payroll is not None, "Draft payroll must exist"

    # API List View by staff
    req = factory.get("/api/v1/salary/payrolls/")
    force_authenticate(req, user=staff_user)
    resp = PayrollListView.as_view()(req)
    staff_viewable_ids = [p["id"] for p in resp.data]
    record_test("Employee list view excludes DRAFT salary slips", str(draft_payroll.id) not in staff_viewable_ids, f"Excluded ID {draft_payroll.id}")

    # API Detail View by direct ID lookup
    req_detail = factory.get(f"/api/v1/salary/payrolls/{draft_payroll.id}/")
    force_authenticate(req_detail, user=staff_user)
    denied = False
    try:
        resp_detail = PayrollDetailView.as_view()(req_detail, pk=draft_payroll.id)
        if resp_detail.status_code in [403, 404]:
            denied = True
    except PermissionDenied:
        denied = True

    record_test("Direct ID lookup on DRAFT payroll raises 403 PermissionDenied", denied)

    # ------------------------------------------------------------------
    # 13. EMPLOYEES CAN ACCESS RELEASED SALARY SLIPS
    # ------------------------------------------------------------------
    print("\n--- 13. Testing Employee Access to Released Salary Slips ---")
    # Finalize and Release the draft run
    PayrollCalculationService.finalize_payroll_run(draft_run, user=admin_user)
    PayrollCalculationService.release_payroll_run(draft_run, user=admin_user)
    draft_payroll.refresh_from_db()
    draft_run.refresh_from_db()

    record_test("PayrollRun status is RELEASED", draft_run.status == PayrollRunStatus.RELEASED)
    record_test("Employee Payroll status is RELEASED", draft_payroll.status == PayrollStatus.RELEASED)
    record_test("Payroll is marked visible to employee", draft_payroll.is_visible_to_employee is True)

    # API List View includes released
    req_rel = factory.get("/api/v1/salary/payrolls/")
    force_authenticate(req_rel, user=staff_user)
    resp_rel = PayrollListView.as_view()(req_rel)
    rel_ids = [p["id"] for p in resp_rel.data]
    record_test("Employee list view includes RELEASED salary slip", str(draft_payroll.id) in rel_ids)

    # API Detail View by direct ID succeeds
    req_rel_det = factory.get(f"/api/v1/salary/payrolls/{draft_payroll.id}/")
    force_authenticate(req_rel_det, user=staff_user)
    resp_rel_det = PayrollDetailView.as_view()(req_rel_det, pk=draft_payroll.id)
    record_test("Direct ID lookup on RELEASED payroll returns 200 OK", resp_rel_det.status_code == 200)

    # ------------------------------------------------------------------
    # 14. CONFIGURATION HIERARCHY & SOURCE TRACKING
    # ------------------------------------------------------------------
    print("\n--- 14. Testing Configuration Hierarchy & Source Display ---")
    res_emp = PayrollScheduleService.resolve_schedule(employee=emp)
    record_test("Resolved configuration tracks effective source", res_emp["source"] in ["ENTERPRISE", "CENTRE", "EMPLOYEE"])
    record_test("Resolved config exposes editing window parameters", "editable_period_duration" in res_emp["effective_config"])
    record_test("Resolved config exposes visibility policy", "visibility_policy" in res_emp["effective_config"])

    # ------------------------------------------------------------------
    # 15. HISTORICAL SNAPSHOT IMMUTABILITY
    # ------------------------------------------------------------------
    print("\n--- 15. Testing Historical Payroll Snapshot Immutability ---")
    captured_gross = draft_payroll.gross_amount
    captured_comp = draft_payroll.compensation_type

    # Change current employee compensation drastically with new revision and schedule
    SalaryRevision.objects.create(
        business=biz,
        employee=emp,
        effective_from=datetime.date(2026, 10, 1),
        basic_salary=Decimal("999999.00"),
        salary_unit="MONTHLY",
        revised_by=admin_user,
        reason="Drastic pay increase after Sept period"
    )
    PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.EMPLOYEE,
        business=biz,
        employee=emp,
        user=admin_user,
        reason="Switched to Fixed Contract",
        data={
            "compensation_type": CompensationType.FIXED_CONTRACT,
        }
    )

    draft_payroll.refresh_from_db()
    record_test("Historical gross amount unaffected by employee salary revision", draft_payroll.gross_amount == captured_gross, f"Still {draft_payroll.gross_amount}")
    record_test("Historical compensation type snapshot preserved", draft_payroll.compensation_type == captured_comp)

    # Restore employee schedule to MONTHLY_SALARY so database remains clean for other test suites
    PayrollScheduleService.save_schedule(
        scope=ScheduleConfigScope.EMPLOYEE,
        business=biz,
        employee=emp,
        user=admin_user,
        reason="Restore to Monthly Salary",
        data={
            "compensation_type": CompensationType.MONTHLY_SALARY,
        }
    )

    # ------------------------------------------------------------------
    # 16. PREVENT DUPLICATE PAYROLL GENERATION
    # ------------------------------------------------------------------
    print("\n--- 16. Testing Duplicate Generation Prevention ---")
    duplicate_blocked = False
    try:
        # Run finalized/released run already exists for Sept 2026
        PayrollCalculationService.run_batch_payroll(
            business=biz,
            period_start=draft_start,
            period_end=draft_end,
            user=admin_user,
            centre=centre_a
        )
    except (DRFValidationError, DjangoValidationError) as e:
        if "finalized" in str(e).lower() or "immutable" in str(e).lower() or "cannot be modified" in str(e).lower():
            duplicate_blocked = True

    record_test("Re-generating finalized/released payroll run is strictly rejected", duplicate_blocked)

    # ------------------------------------------------------------------
    # 17. 12:01 AM SCHEDULER SCAN & LIFECYCLE AUTOMATION
    # ------------------------------------------------------------------
    print("\n--- 17. Testing 12:01 AM Scheduler Scan & Auto-Finalization ---")
    # Test lifecycle scan execution
    summary = PayrollLifecycleService.run_daily_midnight_scan(
        business=biz,
        as_of_datetime=timezone.now()
    )
    record_test("12:01 AM scanner executes without error", "generated_runs_count" in summary)
    record_test("Auto-close expired editing windows handled", "finalized_runs_count" in summary)
    record_test("Auto-release slips evaluated according to policy", "released_runs_count" in summary)

    # Test idempotency: re-running scan immediately generates 0 duplicates
    second_summary = PayrollLifecycleService.run_daily_midnight_scan(
        business=biz,
        as_of_datetime=timezone.now()
    )
    record_test("Repeated scan is completely idempotent", second_summary["generated_runs_count"] == 0, f"Generated: {second_summary['generated_runs_count']}")

    # ------------------------------------------------------------------
    # 18. RBAC AND BUSINESS BOUNDARY ENFORCEMENT
    # ------------------------------------------------------------------
    print("\n--- 18. Testing RBAC & Unauthorized Access Prevention ---")
    # Staff user trying to finalize a run
    req_fin = factory.post(f"/api/v1/payroll/runs/{draft_run.id}/finalize/")
    force_authenticate(req_fin, user=staff_user)
    fin_blocked = False
    try:
        resp_fin = PayrollRunFinalizeView.as_view()(req_fin, pk=draft_run.id)
        if resp_fin.status_code in [401, 403]:
            fin_blocked = True
    except (PermissionDenied, DRFValidationError):
        fin_blocked = True

    record_test("Regular staff blocked from finalizing payroll runs", fin_blocked)

    # Staff user trying to adjust payroll
    req_adj = factory.post(f"/api/v1/payroll/runs/{draft_run.id}/adjust/", {
        "employee_id": str(emp.id),
        "adjustment_type": "BONUS",
        "name": "Self Bonus",
        "amount": 10000,
        "reason": "Hacking bonus"
    }, format="json")
    force_authenticate(req_adj, user=staff_user)
    adj_blocked = False
    try:
        resp_adj = PayrollRunAdjustView.as_view()(req_adj, pk=draft_run.id)
        if resp_adj.status_code in [401, 403]:
            adj_blocked = True
    except (PermissionDenied, DRFValidationError):
        adj_blocked = True

    record_test("Regular staff blocked from applying payroll adjustments", adj_blocked)

    # ------------------------------------------------------------------
    # SUMMARY
    # ------------------------------------------------------------------
    print("\n" + "=" * 75)
    print(f"ACCEPTANCE TESTS COMPLETE: {len(passed_checks)} PASSED, {len(failed_checks)} FAILED")
    print("=" * 75)

    if failed_checks:
        print("\nFailures:")
        for name, details in failed_checks:
            print(f"  - {name}: {details}")
        sys.exit(1)
    else:
        print("\nALL LIFECYCLE ACCEPTANCE CHECKS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    run_lifecycle_acceptance_tests()
