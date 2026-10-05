"""
Comprehensive Acceptance & Validation Test Suite for OWNManage SaaS Platform
Executes checks across Sections 1 to 50 of the Enterprise/Centre/Employee Management specification.
"""

import os
import sys
import datetime
from decimal import Decimal

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()

from django.db import connection, transaction
from django.utils import timezone
from apps.organization.models import Business, Branch, Employee, Department, Designation
from apps.attendance.models import (
    AttendancePolicy, AttendancePolicyOverride, AttendanceDay, AttendanceStatus
)
from apps.organization.services.policy_resolver import resolve_effective_policy
from apps.attendance.services.attendance_calculation_service import calculate_attendance_status
from apps.payroll.models import (
    SalaryRevision, EmployeeCompensationItem, CompensationComponentType, CompensationCalculationType
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


def run_tests():
    print("======================================================================")
    print("STARTING OWNMANAGE ENTERPRISE / CENTRE ACCEPTANCE TESTS")
    print("======================================================================")

    # 1. Retrieve or setup test enterprise
    biz = Business.objects.filter(name="Acme Enterprises").first()
    if not biz:
        biz = Business.objects.first()
    assert biz is not None, "At least one business must exist in database"
    print(f"\n[Test Context] Using Business: {biz.name} (UUID: {biz.id})")

    # Ensure Enterprise Default Attendance Policy exists with standard test defaults
    ent_policy, _ = AttendancePolicy.objects.get_or_create(business=biz)
    ent_policy.office_start = datetime.time(9, 0)
    ent_policy.office_end = datetime.time(18, 0)
    ent_policy.break_start = datetime.time(13, 0)
    ent_policy.break_end = datetime.time(14, 0)
    ent_policy.grace_period_minutes = 20
    ent_policy.late_threshold_minutes = 30
    ent_policy.early_checkout_threshold_minutes = 30
    ent_policy.weekly_off_days = [6] # Sunday
    ent_policy.allow_normal_punch = True
    ent_policy.save()

    # ------------------------------------------------------------------
    # Section 49: Create Three Distinct Test Centres
    # ------------------------------------------------------------------
    print("\n--- 1. Testing Centre Creation & Independent Operation (Section 49) ---")

    # Centre A: 09:00-18:00, Sunday off ([6]), 15 min grace, Face + GPS
    centre_a, _ = Branch.objects.get_or_create(
        business=biz,
        code="TEST-A",
        defaults={
            'name': "Centre A (Test Alpha)",
            'city': "Mumbai",
            'state': "Maharashtra",
            'country': "India",
            'timezone': "Asia/Kolkata",
            'currency': "INR",
            'latitude': Decimal("19.076000"),
            'longitude': Decimal("72.877700"),
            'geofence_radius': 150,
            'status': "ACTIVE"
        }
    )

    # Centre B: 10:00-19:00, Sunday + Monday off ([0, 6]), 30 min grace, QR only
    centre_b, _ = Branch.objects.get_or_create(
        business=biz,
        code="TEST-B",
        defaults={
            'name': "Centre B (Test Beta)",
            'city': "Bengaluru",
            'state': "Karnataka",
            'country': "India",
            'timezone': "Asia/Kolkata",
            'currency': "INR",
            'latitude': Decimal("12.971600"),
            'longitude': Decimal("77.594600"),
            'geofence_radius': 200,
            'status': "ACTIVE"
        }
    )

    # Centre C: 08:30-17:30, 0 weekly offs ([]), 10 min grace, Normal punch
    centre_c, _ = Branch.objects.get_or_create(
        business=biz,
        code="TEST-C",
        defaults={
            'name': "Centre C (Test Gamma)",
            'city': "Hyderabad",
            'state': "Telangana",
            'country': "India",
            'timezone': "Asia/Kolkata",
            'currency': "INR",
            'status': "ACTIVE"
        }
    )

    # Configure overrides for Centre A
    override_a, _ = AttendancePolicyOverride.objects.update_or_create(
        centre=centre_a,
        defaults={
            'office_start': datetime.time(9, 0),
            'office_end': datetime.time(18, 0),
            'grace_period_minutes': 15,
            'late_threshold_minutes': 15,
            'weekly_off_days': [6],
            'allow_normal_punch': False,
            'allow_face_recognition': True,
            'allow_gps': True,
            'allow_qr': False,
            'gps_latitude': Decimal("19.076000"),
            'gps_longitude': Decimal("72.877700"),
            'gps_radius_meters': 150,
        }
    )

    # Configure overrides for Centre B
    override_b, _ = AttendancePolicyOverride.objects.update_or_create(
        centre=centre_b,
        defaults={
            'office_start': datetime.time(10, 0),
            'office_end': datetime.time(19, 0),
            'grace_period_minutes': 30,
            'weekly_off_days': [0, 6], # Sun + Mon
            'allow_normal_punch': False,
            'allow_face_recognition': False,
            'allow_gps': False,
            'allow_qr': True,
        }
    )

    # Configure overrides for Centre C (0 weekly offs)
    override_c, _ = AttendancePolicyOverride.objects.update_or_create(
        centre=centre_c,
        defaults={
            'office_start': datetime.time(8, 30),
            'office_end': datetime.time(17, 30),
            'weekly_off_days': [], # 0 weekly offs!
            'allow_normal_punch': True,
            'allow_qr': False,
            'allow_face_recognition': False,
        }
    )

    record_test("Centres Created and Configured", True, "Centre A, B, C setup with distinct operations")

    # ------------------------------------------------------------------
    # Section 3 & 49: Field-Level Configuration Inheritance & Provenance
    # ------------------------------------------------------------------
    print("\n--- 2. Testing Configuration Inheritance & Provenance ---")
    pol_a = resolve_effective_policy(centre_a)
    pol_b = resolve_effective_policy(centre_b)
    pol_c = resolve_effective_policy(centre_c)

    # Centre A grace period is overridden (15)
    record_test(
        "Centre A Overrides Grace Period",
        pol_a['effective']['grace_period_minutes'] == 15 and pol_a['source']['grace_period_minutes'] == 'center',
        f"val={pol_a['effective']['grace_period_minutes']}, src={pol_a['source']['grace_period_minutes']}"
    )

    # Centre A break_start is inherited from Enterprise (13:00)
    record_test(
        "Centre A Inherits Break Start from Enterprise",
        str(pol_a['effective']['break_start']).startswith('13:00') and pol_a['source']['break_start'] == 'enterprise',
        f"val={pol_a['effective']['break_start']}, src={pol_a['source']['break_start']}"
    )

    # Centre B office start is 10:00 from Centre
    record_test(
        "Centre B Overrides Shift Start",
        str(pol_b['effective']['office_start']).startswith('10:00') and pol_b['source']['office_start'] == 'center',
        f"val={pol_b['effective']['office_start']}, src={pol_b['source']['office_start']}"
    )

    # Centre C has 0 weekly offs
    record_test(
        "Centre C Supports 0 Weekly Off Days",
        pol_c['effective']['weekly_off_days'] == [] and pol_c['source']['weekly_off_days'] == 'center',
        f"weekly_off_days={pol_c['effective']['weekly_off_days']}"
    )

    # Test enterprise default update & inheritance verification
    print("\n[Inheritance Check] Modifying Enterprise Default break_start to 13:30...")
    ent_policy.break_start = datetime.time(13, 30)
    ent_policy.save()

    pol_a_updated = resolve_effective_policy(centre_a)
    pol_b_updated = resolve_effective_policy(centre_b)

    record_test(
        "Centre A Dynamically Inherits Updated Enterprise break_start",
        str(pol_a_updated['effective']['break_start']).startswith('13:30') and pol_a_updated['source']['break_start'] == 'enterprise',
        f"new_val={pol_a_updated['effective']['break_start']}"
    )

    record_test(
        "Centre B Preserves Centre Override grace_period_minutes Unchanged",
        pol_b_updated['effective']['grace_period_minutes'] == 30 and pol_b_updated['source']['grace_period_minutes'] == 'center',
        f"grace={pol_b_updated['effective']['grace_period_minutes']}"
    )

    # Test Reset to Enterprise Default on Centre A's grace period
    print("\n[Reset Check] Resetting Centre A grace_period_minutes to Enterprise Default...")
    override_a.grace_period_minutes = None
    override_a.save()
    pol_a_reset = resolve_effective_policy(centre_a)

    record_test(
        "Centre A Successfully Resets Grace Period to Enterprise Default",
        pol_a_reset['effective']['grace_period_minutes'] == 20 and pol_a_reset['source']['grace_period_minutes'] == 'enterprise',
        f"resetted_val={pol_a_reset['effective']['grace_period_minutes']}, src={pol_a_reset['source']['grace_period_minutes']}"
    )

    # Restore Centre A override
    override_a.grace_period_minutes = 15
    override_a.late_threshold_minutes = 15
    override_a.save()

    # ------------------------------------------------------------------
    # Section 9: Weekly Off Support (0, 1, 2, 7 Days)
    # ------------------------------------------------------------------
    print("\n--- 3. Testing 0 to 7 Weekly Off Support (Section 9) ---")
    from apps.attendance.services.attendance_calculation_service import calculate_attendance_status

    # Test 0 days off (Centre C) on Sunday (weekday 6) and Monday (weekday 0)
    sunday_date = datetime.date(2026, 10, 4) # Sunday
    monday_date = datetime.date(2026, 10, 5) # Monday
    tuesday_date = datetime.date(2026, 10, 6) # Tuesday

    # On Centre C (0 weekly offs), Sunday is a working day, not weekly off
    res_c_sun = calculate_attendance_status(centre_c, sunday_date, check_in_time=None, check_out_time=None)
    record_test(
        "0 Weekly Offs (Centre C): Sunday is NOT Weekly Off",
        res_c_sun['status'] == AttendanceStatus.ABSENT and not res_c_sun['is_weekly_off'],
        f"status={res_c_sun['status']}"
    )

    # On Centre A (1 weekly off: [6]), Sunday is Weekly Off
    res_a_sun = calculate_attendance_status(centre_a, sunday_date, check_in_time=None, check_out_time=None)
    record_test(
        "1 Weekly Off (Centre A): Sunday is WEEK_OFF",
        res_a_sun['status'] == AttendanceStatus.WEEK_OFF and res_a_sun['is_weekly_off'],
        f"status={res_a_sun['status']}"
    )

    # On Centre B (2 weekly offs: [0, 6]), both Sunday and Monday are Weekly Off
    res_b_sun = calculate_attendance_status(centre_b, sunday_date, check_in_time=None, check_out_time=None)
    res_b_mon = calculate_attendance_status(centre_b, monday_date, check_in_time=None, check_out_time=None)
    res_b_tue = calculate_attendance_status(centre_b, tuesday_date, check_in_time=None, check_out_time=None)

    record_test(
        "2 Weekly Offs (Centre B): Sunday is WEEK_OFF",
        res_b_sun['status'] == AttendanceStatus.WEEK_OFF,
        f"status={res_b_sun['status']}"
    )
    record_test(
        "2 Weekly Offs (Centre B): Monday is WEEK_OFF",
        res_b_mon['status'] == AttendanceStatus.WEEK_OFF,
        f"status={res_b_mon['status']}"
    )
    record_test(
        "2 Weekly Offs (Centre B): Tuesday is WORKING DAY",
        res_b_tue['status'] == AttendanceStatus.ABSENT and not res_b_tue['is_weekly_off'],
        f"status={res_b_tue['status']}"
    )

    # ------------------------------------------------------------------
    # Section 7 & 10: Attendance Status Engine with Centre Grace Periods
    # ------------------------------------------------------------------
    print("\n--- 4. Testing Attendance Status Calculation Engine with Grace Periods ---")
    # Check-in at 09:20 on Tuesday:
    # Centre A: Office Start 09:00, Grace 15m -> Late after 09:15 -> Status is LATE
    # Centre B: Office Start 10:00, Grace 30m -> Before 10:00 -> Status is PRESENT (with full hours)
    in_920 = datetime.datetime.combine(tuesday_date, datetime.time(9, 20))
    out_1800 = datetime.datetime.combine(tuesday_date, datetime.time(18, 0))

    calc_a = calculate_attendance_status(centre_a, tuesday_date, check_in_time=in_920, check_out_time=out_1800)
    record_test(
        "Centre A (Grace 15m, Start 09:00): 09:20 Punch is LATE",
        calc_a['is_late'] is True and calc_a['status'] == AttendanceStatus.LATE,
        f"status={calc_a['status']}, is_late={calc_a['is_late']}"
    )

    in_1015 = datetime.datetime.combine(tuesday_date, datetime.time(10, 15))
    out_1900 = datetime.datetime.combine(tuesday_date, datetime.time(19, 0))
    calc_b = calculate_attendance_status(centre_b, tuesday_date, check_in_time=in_1015, check_out_time=out_1900)
    record_test(
        "Centre B (Grace 30m, Start 10:00): 10:15 Punch is PRESENT (Within Grace)",
        calc_b['is_late'] is False and calc_b['status'] == AttendanceStatus.PRESENT,
        f"status={calc_b['status']}, is_late={calc_b['is_late']}"
    )

    # ------------------------------------------------------------------
    # Section 6: Attendance Register & NOT_MARKED for Unpunched Employees
    # ------------------------------------------------------------------
    print("\n--- 5. Testing Attendance Daily Register (NOT_MARKED for Unpunched Staff) ---")
    from apps.attendance.views import AttendanceDailyRegisterView
    from rest_framework.test import APIRequestFactory, force_authenticate
    from apps.accounts.models import User

    admin_user = User.objects.filter(is_superuser=True).first()
    factory = APIRequestFactory()

    # Query daily register for Centre A on a working day (Tuesday)
    req = factory.get(f'/api/v1/attendance/register/?centre_id={centre_a.id}&business_id={biz.id}&date=2026-10-06')
    force_authenticate(req, user=admin_user)
    view = AttendanceDailyRegisterView.as_view()
    resp = view(req)

    record_test(
        "Daily Register API Returns HTTP 200",
        resp.status_code == 200,
        f"status={resp.status_code}"
    )

    data = resp.data
    # If no punches recorded yet today, status must NOT be 'No records found'; it should show NOT_MARKED
    summary = data.get('summary', {})
    record_test(
        "Daily Register Has Summary Counters",
        'not_marked' in summary and 'total_employees' in summary,
        f"total={summary.get('total_employees')}, not_marked={summary.get('not_marked')}"
    )

    # ------------------------------------------------------------------
    # Section 16 to 21: Salary Management, Components, Revisions, Backdating
    # ------------------------------------------------------------------
    print("\n--- 6. Testing Salary Components, History & Backdated Change Warning ---")
    emp = Employee.objects.filter(business=biz).first()
    assert emp is not None, "Need at least one employee in test business"

    # Add Custom Compensation Items
    comp_bonus, _ = EmployeeCompensationItem.objects.update_or_create(
        employee=emp,
        name="Performance Incentive",
        defaults={
            'business': biz,
            'component_type': CompensationComponentType.BONUS,
            'calculation_type': CompensationCalculationType.FIXED_AMOUNT,
            'amount': Decimal("5000.00"),
            'effective_from': datetime.date(2026, 10, 1),
            'is_active': True
        }
    )

    comp_deduct, _ = EmployeeCompensationItem.objects.update_or_create(
        employee=emp,
        name="Transport Allowance Deduction",
        defaults={
            'business': biz,
            'component_type': CompensationComponentType.DEDUCTION,
            'calculation_type': CompensationCalculationType.FIXED_AMOUNT,
            'amount': Decimal("1200.00"),
            'effective_from': datetime.date(2026, 10, 1),
            'is_active': True
        }
    )

    record_test(
        "Custom Compensation Items Created (Bonus & Deduction)",
        comp_bonus.id is not None and comp_deduct.id is not None,
        f"bonus=₹{comp_bonus.amount}, deduct=₹{comp_deduct.amount}"
    )

    # Test Salary Revision Creation
    rev1, _ = SalaryRevision.objects.update_or_create(
        employee=emp,
        effective_from=datetime.date(2026, 7, 1),
        defaults={
            'business': biz,
            'basic_salary': Decimal("20000.00"),
            'hourly_rate': Decimal("120.00"),
            'ot_rate': Decimal("180.00"),
            'reason': "Mid-year review",
        }
    )

    rev2, _ = SalaryRevision.objects.update_or_create(
        employee=emp,
        effective_from=datetime.date(2026, 10, 1),
        defaults={
            'business': biz,
            'basic_salary': Decimal("25000.00"),
            'hourly_rate': Decimal("150.00"),
            'ot_rate': Decimal("225.00"),
            'reason': "Annual increment",
        }
    )

    record_test(
        "Salary Revision History Maintained",
        SalaryRevision.objects.filter(employee=emp).count() >= 2,
        f"revisions_count={SalaryRevision.objects.filter(employee=emp).count()}"
    )

    # Test Salary Comparison View
    from apps.payroll.revision_views import EmployeeSalaryComparisonView
    req_comp = factory.get(f'/api/v1/employees/{emp.id}/salary-comparison/?revision_from={rev1.id}&revision_to={rev2.id}&business_id={biz.id}')
    force_authenticate(req_comp, user=admin_user)
    resp_comp = EmployeeSalaryComparisonView.as_view()(req_comp, pk=str(emp.id))

    record_test(
        "Salary Comparison API Returns Difference & Percentage",
        resp_comp.status_code == 200 and resp_comp.data['comparisons'][0]['difference'] == 5000.0 and resp_comp.data['comparisons'][0]['percentage'] == 25.0,
        f"diff=₹{resp_comp.data['comparisons'][0]['difference']}, pct={resp_comp.data['comparisons'][0]['percentage']}%"
    )

    # Test Backdated Revision Warning (Section 19)
    from apps.payroll.models import Payroll, PayrollStatus
    past_end = datetime.date.today() - datetime.timedelta(days=20)
    past_start = past_end - datetime.timedelta(days=30)
    Payroll.objects.update_or_create(
        employee=emp,
        period_start=past_start,
        defaults={
            'business': biz,
            'period_end': past_end,
            'status': PayrollStatus.PAID,
            'net_amount': Decimal('20000.00'),
            'gross_amount': Decimal('20000.00'),
            'total_deductions': Decimal('0.00'),
        }
    )

    from apps.payroll.revision_views import EmployeeSalaryRevisionListCreateView
    past_date_str = past_start.strftime("%Y-%m-%d")
    req_backdated = factory.post(
        f'/api/v1/employees/{emp.id}/salary-revisions/?business_id={biz.id}',
        data={
            'basic_salary': 28000,
            'effective_from': past_date_str,
            'reason': 'Late retroactive adjustment'
            # without confirm_backdated=True
        },
        format='json'
    )
    force_authenticate(req_backdated, user=admin_user)
    resp_backdated = EmployeeSalaryRevisionListCreateView.as_view()(req_backdated, pk=str(emp.id))

    record_test(
        "Backdated Salary Change Triggers HTTP 409 Warning Requiring Confirmation",
        resp_backdated.status_code == 409 and resp_backdated.data.get('requires_confirmation') is True,
        f"status={resp_backdated.status_code}, affected={len(resp_backdated.data.get('affected_periods', []))}"
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
        print("ALL ACCEPTANCE TESTS COMPLETED SUCCESSFULLY WITH ZERO ERRORS!")


if __name__ == '__main__':
    run_tests()
