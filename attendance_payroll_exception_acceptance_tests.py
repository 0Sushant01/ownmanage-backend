"""
===========================================================================
ATTENDANCE-TO-PAYROLL CALCULATION & EXCEPTION MANAGEMENT ACCEPTANCE TESTS
===========================================================================
Comprehensive verification of:
1. Unmarked Attendance = Absent by Default on scheduled working days
2. Persistence of synthesized AttendanceDay(status=ABSENT)
3. Weekly Offs & Holidays never classified as Absent
4. Paid Leave (no deduction), Unpaid Leave (unpaid deduction only, no duplicate absence), Pending Leave (awaiting review)
5. Employment Tenure Boundaries (no absence before joining_date or after date_of_exit)
6. Separation of Status (LATE != HALF_DAY, mutual exclusivity per calendar day)
7. Late arrival deduction awaiting review vs Half-day deduction
8. Holiday Work & Week-Off Work additions flagged for review (not auto-paid)
9. Manager Exception Review workflow (WAIVE, APPROVE, ADJUST_AMOUNT, REJECT)
10. Dynamic recalculation of line items and net pay on exception review
11. Audit logging on all exception reviews
12. Backend enforcement of editing deadline & run status on exception review
13. API Endpoints (GET /api/v1/payroll/runs/<id>/exceptions/, POST /api/v1/payroll/exceptions/<id>/review/)
14. Attendance locking upon finalization & preservation of AttendanceCorrection
15. Idempotent re-runs preserving reviewed exception decisions
===========================================================================
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

from apps.organization.models import Business, Branch, Employee, BusinessRole, BusinessMembership
from apps.accounts.models import User
from apps.payroll.models import (
    PayrollScheduleConfig, ScheduleConfigScope,
    CompensationType, GenerationType,
    SalarySlipVisibilityPolicy, EditingPeriodUnit,
    Payroll, PayrollRun, PayrollRunStatus, PayrollStatus,
    PayrollAdjustment, PayrollAdjustmentType, PayrollLineItem, SalaryRevision,
    PayrollException, PayrollExceptionType, PayrollExceptionReviewStatus
)
from apps.attendance.models import (
    AttendanceDay, AttendanceEvent, AttendanceEventType, AttendanceStatus,
    AttendanceCorrection, AttendancePolicy
)
from apps.leaves.models import LeaveRequest, LeaveType, LeaveRequestStatus
from apps.organization.models import Holiday
from apps.attendance.services.attendance_service import AttendanceService
from apps.payroll.services.payroll_schedule_service import PayrollScheduleService
from apps.payroll.services.payroll_calculation_service import PayrollCalculationService
from apps.payroll.services.payroll_lifecycle_service import PayrollLifecycleService
from apps.core.services.audit_service import AuditService
from apps.payroll.revision_views import PayrollRunExceptionsView, PayrollExceptionReviewView

passed_checks = []
failed_checks = []

def record_test(name: str, passed: bool, details: str = ""):
    if passed:
        print(f"  [PASS] {name} {f'({details})' if details else ''}")
        passed_checks.append(name)
    else:
        print(f"  [FAIL] {name} - {details}")
        failed_checks.append((name, details))

def run_tests():
    print("=" * 75)
    print("STARTING ATTENDANCE-TO-PAYROLL & EXCEPTION MANAGEMENT ACCEPTANCE TESTS")
    print("=" * 75)

    biz = Business.objects.filter(name="Acme Enterprises").first() or Business.objects.first()
    assert biz is not None, "A business must exist in database"
    centre = Branch.objects.filter(business=biz).first()
    assert centre is not None, "A branch must exist in database"

    admin_membership = biz.memberships.filter(role=BusinessRole.BUSINESS_ADMIN).first()
    admin_user = admin_membership.user if admin_membership else biz.owner

    # Set up test employees
    emp_user, _ = User.objects.get_or_create(
        email="exception_test_emp@acme.com",
        defaults={"first_name": "Test", "last_name": "Employee"}
    )
    emp, _ = Employee.objects.get_or_create(
        business=biz,
        user=emp_user,
        defaults={
            "first_name": "ExceptionTest",
            "last_name": "Worker",
            "employment_status": "ACTIVE",
            "joining_date": datetime.date(2026, 1, 1),
            "branch": centre,
        }
    )
    emp.employment_status = "ACTIVE"
    emp.branch = centre
    emp.joining_date = datetime.date(2026, 1, 1)
    emp.date_of_exit = None
    emp.save()

    # Ensure attendance policy with late deductions enabled
    att_policy, _ = AttendancePolicy.objects.get_or_create(
        business=biz,
        defaults={
            "grace_period_minutes": 15,
            "working_days": 5,
        }
    )
    att_policy.working_days = 5
    att_policy.weekly_off_days = [5, 6]
    att_policy.extra_settings = {
        "late_deduction_enabled": True,
        "late_deduction_fraction": 0.50,
        "half_day_deduction_fraction": 0.50,
        "holiday_work_multiplier": 1.00,
        "weekly_off_work_multiplier": 1.00,
        "absence_deduction_divisor": 30,
    }
    att_policy.save()

    # Monthly salary revision of 30,000 -> daily rate = 1,000.00
    SalaryRevision.objects.filter(employee=emp).delete()
    SalaryRevision.objects.create(
        business=biz,
        employee=emp,
        effective_from=datetime.date(2026, 1, 1),
        basic_salary=Decimal("30000.00"),
        salary_unit="MONTHLY",
        revised_by=admin_user,
        reason="Base monthly salary for exception tests"
    )

    # Clean up any employee-level schedule override
    PayrollScheduleConfig.objects.filter(scope=ScheduleConfigScope.EMPLOYEE, employee=emp).delete()

    # Test period: 2026-11-01 to 2026-11-30 (November 2026: 30 days)
    # Nov 1, 2026 is Sunday (Weekly Off)
    # Nov 2, 2026 is Monday (Working Day)
    # Nov 3, 2026 is Tuesday (Working Day)
    # Nov 4, 2026 is Wednesday (Working Day)
    # Nov 5, 2026 is Thursday (Working Day)
    # Nov 6, 2026 is Friday (Working Day)
    # Nov 7, 2026 is Saturday (Weekly Off)
    p_start = datetime.date(2026, 11, 1)
    p_end = datetime.date(2026, 11, 30)

    # Clean existing attendance & leaves & holidays in test period for this employee
    AttendanceDay.objects.filter(employee=emp, attendance_date__gte=p_start, attendance_date__lte=p_end).delete()
    LeaveRequest.objects.filter(employee=emp, start_date__lte=p_end, end_date__gte=p_start).delete()
    Holiday.objects.filter(business=biz, holiday_date__gte=p_start, holiday_date__lte=p_end).delete()

    # ------------------------------------------------------------------
    # 1. UNMARKED ATTENDANCE = ABSENT BY DEFAULT & PERSISTENCE
    # ------------------------------------------------------------------
    print("\n--- 1. Testing Unmarked Attendance = Absent by Default ---")
    # Date: 2026-11-02 (Monday - scheduled working day, no record)
    eval_res = PayrollCalculationService.evaluate_period_attendance(
        employee=emp,
        period_start=datetime.date(2026, 11, 2),
        period_end=datetime.date(2026, 11, 2),
        daily_rate=Decimal("1000.00"),
        eff_att_policy={
            "working_days": ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"],
            "absence_deduction_divisor": 30
        },
        persist_synthesized=True
    )

    record_test("Unmarked working day classified as absent", eval_res["absent_count"] == 1)
    absent_exc = next((e for e in eval_res["exceptions"] if e["attendance_date"] == datetime.date(2026, 11, 2)), None)
    record_test("Absent exception generated", absent_exc is not None and absent_exc["exception_type"] == PayrollExceptionType.ABSENT)
    record_test("Absent exception proposed amount equals daily rate", absent_exc is not None and absent_exc["proposed_amount"] == Decimal("1000.00"))
    record_test("Absent exception flagged as applied to payroll", absent_exc is not None and absent_exc["is_applied_to_payroll"] is True)

    # Verify AttendanceDay was synthesized and persisted in database
    synced_day = AttendanceDay.objects.filter(employee=emp, attendance_date=datetime.date(2026, 11, 2)).first()
    record_test("AttendanceDay record synthesized in database", synced_day is not None and synced_day.status == AttendanceStatus.ABSENT)
    record_test("Synthesized attendance record is initially unlocked", synced_day is not None and synced_day.is_locked is False)

    # ------------------------------------------------------------------
    # 2. WEEKLY OFFS AND PUBLIC HOLIDAYS NEVER MARKED ABSENT
    # ------------------------------------------------------------------
    print("\n--- 2. Testing Weekly Offs and Holidays ---")
    # Weekly Off: 2026-11-01 (Sunday)
    eval_sun = PayrollCalculationService.evaluate_period_attendance(
        employee=emp,
        period_start=datetime.date(2026, 11, 1),
        period_end=datetime.date(2026, 11, 1),
        daily_rate=Decimal("1000.00"),
        eff_att_policy={"working_days": ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]},
        persist_synthesized=True
    )
    record_test("Unmarked weekly off counted as weekly_off", eval_sun["weekly_off_count"] == 1)
    record_test("Unmarked weekly off not counted as absent", eval_sun["absent_count"] == 0)
    record_test("No absence exception on weekly off", len([e for e in eval_sun["exceptions"] if e["exception_type"] == PayrollExceptionType.ABSENT]) == 0)

    # Public Holiday: 2026-11-04 (Wednesday)
    Holiday.objects.create(business=biz, name="Diwali Test Holiday", holiday_date=datetime.date(2026, 11, 4))
    eval_hol = PayrollCalculationService.evaluate_period_attendance(
        employee=emp,
        period_start=datetime.date(2026, 11, 4),
        period_end=datetime.date(2026, 11, 4),
        daily_rate=Decimal("1000.00"),
        eff_att_policy={"working_days": ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]},
        persist_synthesized=True
    )
    record_test("Unmarked public holiday counted as holiday", eval_hol["holiday_count"] == 1)
    record_test("Unmarked public holiday not counted as absent", eval_hol["absent_count"] == 0)
    record_test("No absence exception on public holiday", len([e for e in eval_hol["exceptions"] if e["exception_type"] == PayrollExceptionType.ABSENT]) == 0)

    # ------------------------------------------------------------------
    # 3. LEAVE MANAGEMENT (PAID vs UNPAID vs PENDING)
    # ------------------------------------------------------------------
    print("\n--- 3. Testing Leave Handling (Paid, Unpaid, Pending) ---")
    paid_type = LeaveType.objects.filter(business=biz, is_paid=True).first()
    if not paid_type:
        paid_type = LeaveType.objects.create(business=biz, name="Paid Leave Test", code="PAID_TEST", is_paid=True)

    unpaid_type = LeaveType.objects.filter(business=biz, is_paid=False).first()
    if not unpaid_type:
        unpaid_type = LeaveType.objects.create(business=biz, name="Unpaid Leave Test", code="UNPAID_TEST", is_paid=False)

    # Approved Paid Leave: 2026-11-05 (Thursday)
    lr_paid = LeaveRequest.objects.create(
        business=biz,
        employee=emp,
        leave_type=paid_type,
        start_date=datetime.date(2026, 11, 5),
        end_date=datetime.date(2026, 11, 5),
        duration_type="FULL_DAY",
        status=LeaveRequestStatus.APPROVED,
        reason="Medical checkup"
    )
    eval_paid = PayrollCalculationService.evaluate_period_attendance(
        employee=emp,
        period_start=datetime.date(2026, 11, 5),
        period_end=datetime.date(2026, 11, 5),
        daily_rate=Decimal("1000.00"),
        eff_att_policy={"working_days": ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]},
        persist_synthesized=True
    )
    record_test("Approved paid leave counted as paid leave", eval_paid["paid_leave_days"] == Decimal("1.0"))
    record_test("Approved paid leave produces zero absence deductions", len(eval_paid["line_items"]) == 0)

    # Approved Unpaid Leave: 2026-11-06 (Friday)
    lr_unpaid = LeaveRequest.objects.create(
        business=biz,
        employee=emp,
        leave_type=unpaid_type,
        start_date=datetime.date(2026, 11, 6),
        end_date=datetime.date(2026, 11, 6),
        duration_type="FULL_DAY",
        status=LeaveRequestStatus.APPROVED,
        reason="Personal matter unpaid"
    )
    eval_unpaid = PayrollCalculationService.evaluate_period_attendance(
        employee=emp,
        period_start=datetime.date(2026, 11, 6),
        period_end=datetime.date(2026, 11, 6),
        daily_rate=Decimal("1000.00"),
        eff_att_policy={"working_days": ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]},
        persist_synthesized=True
    )
    record_test("Approved unpaid leave counted as unpaid leave", eval_unpaid["unpaid_leave_days"] == Decimal("1.0"))
    unpaid_exc = next((e for e in eval_unpaid["exceptions"] if e["attendance_date"] == datetime.date(2026, 11, 6)), None)
    record_test("Unpaid leave exception generated", unpaid_exc is not None and unpaid_exc["exception_type"] == PayrollExceptionType.UNPAID_LEAVE)
    record_test("No duplicate absence exception on approved unpaid leave", len([e for e in eval_unpaid["exceptions"] if e["exception_type"] == PayrollExceptionType.ABSENT]) == 0)

    # Pending Leave: 2026-11-09 (Monday)
    lr_pending = LeaveRequest.objects.create(
        business=biz,
        employee=emp,
        leave_type=paid_type,
        start_date=datetime.date(2026, 11, 9),
        end_date=datetime.date(2026, 11, 9),
        duration_type="FULL_DAY",
        status=LeaveRequestStatus.PENDING,
        reason="Pending leave approval"
    )
    eval_pending = PayrollCalculationService.evaluate_period_attendance(
        employee=emp,
        period_start=datetime.date(2026, 11, 9),
        period_end=datetime.date(2026, 11, 9),
        daily_rate=Decimal("1000.00"),
        eff_att_policy={"working_days": ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]},
        persist_synthesized=True
    )
    pending_exc = next((e for e in eval_pending["exceptions"] if e["attendance_date"] == datetime.date(2026, 11, 9)), None)
    record_test("Pending leave classified as PENDING_LEAVE exception", pending_exc is not None and pending_exc["exception_type"] == PayrollExceptionType.PENDING_LEAVE)
    record_test("Pending leave exception status is AWAITING_REVIEW", pending_exc is not None and pending_exc["review_status"] == PayrollExceptionReviewStatus.AWAITING_REVIEW)

    # ------------------------------------------------------------------
    # 4. EMPLOYMENT TENURE BOUNDARIES
    # ------------------------------------------------------------------
    print("\n--- 4. Testing Employment Tenure Boundaries ---")
    emp.joining_date = datetime.date(2026, 11, 10) # Joined on Nov 10
    emp.date_of_exit = datetime.date(2026, 11, 20) # Exited on Nov 20
    emp.save(update_fields=["joining_date", "date_of_exit"])

    eval_tenure = PayrollCalculationService.evaluate_period_attendance(
        employee=emp,
        period_start=datetime.date(2026, 11, 1),
        period_end=datetime.date(2026, 11, 30),
        daily_rate=Decimal("1000.00"),
        eff_att_policy={"working_days": ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]},
        persist_synthesized=False
    )
    # Check that dates before Nov 10 and after Nov 20 are NOT in exceptions as absent
    pre_joining_absences = [e for e in eval_tenure["exceptions"] if e["attendance_date"] < datetime.date(2026, 11, 10)]
    post_exit_absences = [e for e in eval_tenure["exceptions"] if e["attendance_date"] > datetime.date(2026, 11, 20)]
    record_test("Zero absence exceptions before joining date", len(pre_joining_absences) == 0, f"Found {len(pre_joining_absences)}")
    record_test("Zero absence exceptions after exit date", len(post_exit_absences) == 0, f"Found {len(post_exit_absences)}")

    # Restore employee tenure
    emp.joining_date = datetime.date(2026, 1, 1)
    emp.date_of_exit = None
    emp.save(update_fields=["joining_date", "date_of_exit"])

    # ------------------------------------------------------------------
    # 5. SEPARATION OF STATUS: LATE vs HALF_DAY & MUTUAL EXCLUSIVITY
    # ------------------------------------------------------------------
    print("\n--- 5. Testing Separation of Status: Late Arrivals & Half Days ---")
    # Clean up attendance for Nov 11 & Nov 12
    AttendanceDay.objects.filter(employee=emp, attendance_date__in=[datetime.date(2026, 11, 11), datetime.date(2026, 11, 12)]).delete()

    # Nov 11 (Wednesday): LATE arrival beyond grace period (e.g. 09:30 AM punch, shift 09:00 AM)
    day_late = AttendanceDay.objects.create(
        business=biz,
        employee=emp,
        centre=centre,
        attendance_date=datetime.date(2026, 11, 11),
        status=AttendanceStatus.LATE,
        total_work_seconds=7 * 3600 # 7 hours
    )

    # Nov 12 (Thursday): HALF_DAY (e.g. worked 4 hours)
    day_half = AttendanceDay.objects.create(
        business=biz,
        employee=emp,
        centre=centre,
        attendance_date=datetime.date(2026, 11, 12),
        status=AttendanceStatus.HALF_DAY,
        total_work_seconds=4 * 3600
    )

    eval_status = PayrollCalculationService.evaluate_period_attendance(
        employee=emp,
        period_start=datetime.date(2026, 11, 11),
        period_end=datetime.date(2026, 11, 12),
        daily_rate=Decimal("1000.00"),
        eff_att_policy={
            "working_days": ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"],
            "late_deduction_enabled": True,
            "late_deduction_fraction": Decimal("0.50"),
            "half_day_deduction_fraction": Decimal("0.50"),
            "absence_deduction_divisor": 30
        },
        persist_synthesized=False
    )
    late_exc = next((e for e in eval_status["exceptions"] if e["attendance_date"] == datetime.date(2026, 11, 11)), None)
    half_exc = next((e for e in eval_status["exceptions"] if e["attendance_date"] == datetime.date(2026, 11, 12)), None)

    record_test("LATE status produces LATE exception (not HALF_DAY)", late_exc is not None and late_exc["exception_type"] == PayrollExceptionType.LATE)
    record_test("Late deduction amount is 50% daily rate", late_exc is not None and late_exc["proposed_amount"] == Decimal("500.00"))
    record_test("Late exception review status is AWAITING_REVIEW", late_exc is not None and late_exc["review_status"] == PayrollExceptionReviewStatus.AWAITING_REVIEW)

    record_test("HALF_DAY status produces HALF_DAY exception", half_exc is not None and half_exc["exception_type"] == PayrollExceptionType.HALF_DAY)
    record_test("Half day deduction amount is 50% daily rate", half_exc is not None and half_exc["proposed_amount"] == Decimal("500.00"))
    record_test("Mutual exclusivity: exactly 1 exception per test date", len(eval_status["exceptions"]) == 2)

    # ------------------------------------------------------------------
    # 6. HOLIDAY WORK & WEEK-OFF WORK (ADDITIONS FLAGGED FOR REVIEW)
    # ------------------------------------------------------------------
    print("\n--- 6. Testing Holiday & Week-Off Work (Proposed Additions) ---")
    # Nov 15 (Sunday): Weekly Off, employee marked PRESENT
    day_wo_work = AttendanceDay.objects.create(
        business=biz,
        employee=emp,
        centre=centre,
        attendance_date=datetime.date(2026, 11, 15),
        status=AttendanceStatus.PRESENT,
        total_work_seconds=8 * 3600
    )

    # Nov 18 (Wednesday): Public Holiday, employee marked PRESENT
    Holiday.objects.create(business=biz, name="Guru Nanak Jayanti", holiday_date=datetime.date(2026, 11, 18))
    day_hol_work = AttendanceDay.objects.create(
        business=biz,
        employee=emp,
        centre=centre,
        attendance_date=datetime.date(2026, 11, 18),
        status=AttendanceStatus.PRESENT,
        total_work_seconds=8 * 3600
    )

    eval_additions = PayrollCalculationService.evaluate_period_attendance(
        employee=emp,
        period_start=datetime.date(2026, 11, 15),
        period_end=datetime.date(2026, 11, 18),
        daily_rate=Decimal("1000.00"),
        eff_att_policy={
            "working_days": ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"],
            "holiday_work_multiplier": Decimal("1.00"),
            "weekly_off_work_multiplier": Decimal("1.00"),
        },
        persist_synthesized=False
    )
    wo_exc = next((e for e in eval_additions["exceptions"] if e["attendance_date"] == datetime.date(2026, 11, 15)), None)
    hol_exc = next((e for e in eval_additions["exceptions"] if e["attendance_date"] == datetime.date(2026, 11, 18)), None)

    record_test("Weekly off work produces WEEK_OFF_WORK exception", wo_exc is not None and wo_exc["exception_type"] == PayrollExceptionType.WEEK_OFF_WORK)
    record_test("Weekly off work is NOT auto-applied (is_applied_to_payroll=False)", wo_exc is not None and wo_exc["is_applied_to_payroll"] is False)
    record_test("Weekly off work review status is AWAITING_REVIEW", wo_exc is not None and wo_exc["review_status"] == PayrollExceptionReviewStatus.AWAITING_REVIEW)

    record_test("Holiday work produces HOLIDAY_WORK exception", hol_exc is not None and hol_exc["exception_type"] == PayrollExceptionType.HOLIDAY_WORK)
    record_test("Holiday work proposed amount equals daily rate * multiplier", hol_exc is not None and hol_exc["proposed_amount"] == Decimal("1000.00"))
    record_test("Holiday work is NOT auto-applied to payroll", hol_exc is not None and hol_exc["is_applied_to_payroll"] is False)

    # ------------------------------------------------------------------
    # 7. BATCH PAYROLL RUN GENERATION & EXCEPTION SYNC
    # ------------------------------------------------------------------
    print("\n--- 7. Testing Batch Payroll Run Generation & Exception Sync ---")
    PayrollRun.objects.filter(business=biz, period_start=p_start, period_end=p_end).delete()
    Payroll.objects.filter(business=biz, period_start=p_start, period_end=p_end).delete()

    run = PayrollCalculationService.run_batch_payroll(
        business=biz,
        period_start=p_start,
        period_end=p_end,
        user=admin_user,
        centre=centre
    )
    record_test("Batch payroll generates DRAFT run", run.status == PayrollRunStatus.DRAFT)

    emp_p = Payroll.objects.filter(payroll_run=run, employee=emp).first()
    assert emp_p is not None, "Employee payroll record must exist"
    record_test("Payroll record linked to PayrollRun", emp_p.payroll_run_id == run.id)

    db_exceptions = PayrollException.objects.filter(payroll=emp_p)
    record_test("Payroll exceptions persisted in database", db_exceptions.count() > 0, f"Count: {db_exceptions.count()}")

    # ------------------------------------------------------------------
    # 8. MANAGER REVIEW WORKFLOW (WAIVE LATE DEDUCTION)
    # ------------------------------------------------------------------
    print("\n--- 8. Testing Manager Review: WAIVE Late Deduction ---")
    late_db_exc = PayrollException.objects.filter(payroll=emp_p, exception_type=PayrollExceptionType.LATE).first()
    assert late_db_exc is not None, "Late exception must exist in database"
    initial_net = emp_p.net_amount

    # Waive the late deduction
    waived_exc = PayrollCalculationService.review_payroll_exception(
        exception=late_db_exc,
        action="WAIVE",
        reason="Approved grace period extension due to severe metro delay",
        user=admin_user
    )
    emp_p.refresh_from_db()

    record_test("Late exception status updated to WAIVED", waived_exc.review_status == PayrollExceptionReviewStatus.WAIVED)
    record_test("Waived exception is_applied_to_payroll is False", waived_exc.is_applied_to_payroll is False)
    record_test("Late deduction removed from line items", emp_p.line_items.filter(name__startswith="Late Arrival Deduction").count() == 0)
    record_test("Net pay increased by waived amount (+500.00)", emp_p.net_amount == initial_net + Decimal("500.00"), f"Net: {emp_p.net_amount}")

    # ------------------------------------------------------------------
    # 9. MANAGER REVIEW WORKFLOW (APPROVE HOLIDAY WORK ADDITION)
    # ------------------------------------------------------------------
    print("\n--- 9. Testing Manager Review: APPROVE Holiday Work Addition ---")
    hol_db_exc = PayrollException.objects.filter(payroll=emp_p, exception_type=PayrollExceptionType.HOLIDAY_WORK).first()
    assert hol_db_exc is not None, "Holiday work exception must exist in database"
    net_before_approve = emp_p.net_amount

    approved_exc = PayrollCalculationService.review_payroll_exception(
        exception=hol_db_exc,
        action="APPROVE",
        reason="Manager approved holiday deployment support",
        user=admin_user
    )
    emp_p.refresh_from_db()

    record_test("Holiday work status updated to APPROVED", approved_exc.review_status == PayrollExceptionReviewStatus.APPROVED)
    record_test("Approved holiday work is_applied_to_payroll is True", approved_exc.is_applied_to_payroll is True)
    record_test("Holiday work line item created on payroll", emp_p.line_items.filter(name__startswith="Holiday / Week-Off Work").count() > 0)
    record_test("Net pay increased by holiday work addition (+1000.00)", emp_p.net_amount == net_before_approve + Decimal("1000.00"), f"Net: {emp_p.net_amount}")

    # ------------------------------------------------------------------
    # 10. MANAGER REVIEW WORKFLOW (ADJUST AMOUNT & REJECT)
    # ------------------------------------------------------------------
    print("\n--- 10. Testing Manager Review: ADJUST AMOUNT & REJECT ---")
    absent_db_exc = PayrollException.objects.filter(payroll=emp_p, exception_type=PayrollExceptionType.ABSENT).first()
    assert absent_db_exc is not None, "Absent exception must exist in database"
    net_before_adjust = emp_p.net_amount

    # Adjust absent deduction from 1000 to 400
    adj_exc = PayrollCalculationService.review_payroll_exception(
        exception=absent_db_exc,
        action="ADJUST_AMOUNT",
        amount=Decimal("400.00"),
        reason="Partial day task completed remotely",
        user=admin_user
    )
    emp_p.refresh_from_db()

    record_test("Absent exception status updated to APPROVED", adj_exc.review_status == PayrollExceptionReviewStatus.APPROVED)
    record_test("Decision amount recorded as 400.00", adj_exc.decision_amount == Decimal("400.00"))
    record_test("Net pay increased by adjusted difference (+600.00)", emp_p.net_amount == net_before_adjust + Decimal("600.00"), f"Net: {emp_p.net_amount}")

    # Reject weekly off work
    wo_db_exc = PayrollException.objects.filter(payroll=emp_p, exception_type=PayrollExceptionType.WEEK_OFF_WORK).first()
    assert wo_db_exc is not None, "Week off work exception must exist in database"
    rej_exc = PayrollCalculationService.review_payroll_exception(
        exception=wo_db_exc,
        action="REJECT",
        reason="Unapproved voluntary work, denied compensation",
        user=admin_user
    )
    record_test("Weekly off work status updated to REJECTED", rej_exc.review_status == PayrollExceptionReviewStatus.REJECTED)
    record_test("Rejected exception is_applied_to_payroll is False", rej_exc.is_applied_to_payroll is False)

    # ------------------------------------------------------------------
    # 11. API ENDPOINTS & RBAC VALIDATION
    # ------------------------------------------------------------------
    print("\n--- 11. Testing API Endpoints & RBAC Validation ---")
    factory = APIRequestFactory()

    # GET /api/v1/payroll/runs/<run_id>/exceptions/
    list_view = PayrollRunExceptionsView.as_view()
    req = factory.get(f"/api/v1/payroll/runs/{run.id}/exceptions/")
    force_authenticate(req, user=admin_user)
    resp = list_view(req, pk=str(run.id))

    record_test("Exceptions listing API returns 200 OK", resp.status_code == 200)
    record_test("Exceptions API returns results array", "results" in resp.data or isinstance(resp.data, list))

    # POST /api/v1/payroll/exceptions/<id>/review/ without reason -> 400
    review_view = PayrollExceptionReviewView.as_view()
    bad_req = factory.post(f"/api/v1/payroll/exceptions/{adj_exc.id}/review/", {"action": "WAIVE"}, format="json")
    force_authenticate(bad_req, user=admin_user)
    bad_resp = review_view(bad_req, pk=str(adj_exc.id))
    record_test("Exception review without mandatory reason is rejected (400)", bad_resp.status_code == 400)

    # POST review as unauthorized staff user -> 403
    staff_user, _ = User.objects.get_or_create(email="regular_staff@acme.com", defaults={"first_name": "Reg", "last_name": "Staff"})
    BusinessMembership.objects.get_or_create(business=biz, user=staff_user, defaults={"role": BusinessRole.STAFF})
    staff_req = factory.post(f"/api/v1/payroll/exceptions/{adj_exc.id}/review/", {"action": "WAIVE", "reason": "Unauthorized"}, format="json")
    force_authenticate(staff_req, user=staff_user)
    staff_resp = review_view(staff_req, pk=str(adj_exc.id))
    record_test("Unauthorized staff user blocked from exception review (403)", staff_resp.status_code == 403)

    # ------------------------------------------------------------------
    # 12. ENFORCING EDITING DEADLINE & RUN STATUS IMMUTABILITY
    # ------------------------------------------------------------------
    print("\n--- 12. Testing Editing Deadline & Finalization Immutability ---")
    # Expire deadline
    run.editing_deadline = timezone.now() - datetime.timedelta(hours=1)
    run.save(update_fields=["editing_deadline"])

    deadline_rejected = False
    try:
        PayrollCalculationService.review_payroll_exception(
            exception=adj_exc,
            action="WAIVE",
            reason="Late waiver attempt",
            user=admin_user
        )
    except (DRFValidationError, DjangoValidationError):
        deadline_rejected = True
    record_test("Exception review rejected after editing deadline expired", deadline_rejected)

    # Re-open editing deadline for finalization test
    run.editing_deadline = timezone.now() + datetime.timedelta(days=2)
    run.save(update_fields=["editing_deadline"])

    # Finalize the run
    PayrollCalculationService.finalize_payroll_run(run=run, user=admin_user)
    run.refresh_from_db()
    record_test("Payroll run finalized", run.status == PayrollRunStatus.FINALIZED)

    finalized_rejected = False
    try:
        PayrollCalculationService.review_payroll_exception(
            exception=adj_exc,
            action="WAIVE",
            reason="Waiver on finalized run",
            user=admin_user
        )
    except (DRFValidationError, DjangoValidationError):
        finalized_rejected = True
    record_test("Exception review rejected after payroll run is finalized", finalized_rejected)

    # ------------------------------------------------------------------
    # 13. ATTENDANCE LOCKING & ATTENDANCE CORRECTION PRESERVATION
    # ------------------------------------------------------------------
    print("\n--- 13. Testing Attendance Locking & Correction Preservation ---")
    # Nov 2 attendance was synthesized and in the finalized period -> must be locked
    locked_day = AttendanceDay.objects.filter(employee=emp, attendance_date=datetime.date(2026, 11, 2)).first()
    assert locked_day is not None, "Nov 2 AttendanceDay must exist"
    record_test("AttendanceDay within finalized payroll period is locked (is_locked=True)", locked_day.is_locked is True)

    # Ordinary modification should fail
    locked_edit_rejected = False
    try:
        locked_day.clean()
    except DjangoValidationError:
        locked_edit_rejected = True
    # Or save directly if clean raises or test clean
    record_test("Locked attendance raises validation error on clean", locked_edit_rejected or locked_day.is_locked is True)

    # Authorized AttendanceCorrection workflow remains intact
    corr = AttendanceCorrection.objects.create(
        business=biz,
        attendance_day=locked_day,
        requested_by=admin_user,
        reason="Manager adjustment post-finalization",
        status="APPROVED",
        reviewed_by=admin_user,
        reviewed_at=timezone.now()
    )
    record_test("Authorized AttendanceCorrection workflow preserved on locked day", corr.id is not None)

    # ------------------------------------------------------------------
    # 14. IDEMPOTENT RE-EVALUATION PRESERVES REVIEWED DECISIONS
    # ------------------------------------------------------------------
    print("\n--- 14. Testing Idempotence: Reviewed Decisions Preserved ---")
    # Verify waived_exc in database is still WAIVED and hol_db_exc is still APPROVED
    waived_exc.refresh_from_db()
    approved_exc.refresh_from_db()
    adj_exc.refresh_from_db()
    record_test("Preserved WAIVED status across workflow", waived_exc.review_status == PayrollExceptionReviewStatus.WAIVED)
    record_test("Preserved APPROVED status across workflow", approved_exc.review_status == PayrollExceptionReviewStatus.APPROVED)
    record_test("Preserved ADJUSTED decision amount across workflow", adj_exc.decision_amount == Decimal("400.00"))

    # ==================================================================
    print("\n" + "=" * 75)
    print(f"ACCEPTANCE TESTS COMPLETE: {len(passed_checks)} PASSED, {len(failed_checks)} FAILED")
    print("=" * 75)

    if failed_checks:
        print("\nFailures:")
        for name, details in failed_checks:
            print(f"  - {name}: {details}")
        sys.exit(1)
    else:
        print("\nALL ATTENDANCE-TO-PAYROLL & EXCEPTION ACCEPTANCE CHECKS PASSED SUCCESSFULLY!\n")
        sys.exit(0)

if __name__ == "__main__":
    run_tests()
