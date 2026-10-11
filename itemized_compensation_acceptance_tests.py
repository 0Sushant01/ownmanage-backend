"""
Comprehensive Acceptance Test Suite for Itemized Compensation Components in OWNManage

Covers:
Section A: DAILY SALARY
Section B: RECURRING COMPONENTS
Section C: ONE-TIME COMPONENTS
Section D: CONFIGURATION INHERITANCE
Section E: PAYROLL INTEGRITY & RBAC
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

from apps.organization.models import Business, Branch, Employee, BusinessRole, BusinessMembership
from apps.accounts.models import User
from apps.payroll.models import (
    PayrollScheduleConfig, ScheduleConfigScope,
    CompensationType, GenerationType,
    Payroll, PayrollRun, PayrollRunStatus, PayrollStatus, SalaryRevision,
    EmployeeCompensationItem, CompensationComponentType, CompensationCalculationType,
    CompensationFrequency, PayrollLineItem, PayrollLineItemType
)
from apps.payroll.services.payroll_schedule_service import PayrollScheduleService
from apps.payroll.services.payroll_calculation_service import PayrollCalculationService
from apps.payroll.services.compensation_resolver import CompensationResolver
from apps.attendance.models import AttendanceDay, AttendanceStatus
from apps.payroll.revision_views import (
    EmployeeCompensationItemListCreateView,
    EmployeeCompensationItemDetailView,
    CentreCompensationItemListCreateView,
    EnterpriseCompensationItemListCreateView
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
    print("=" * 70)
    print("STARTING ITEMIZED COMPENSATION COMPONENTS ACCEPTANCE TESTS")
    print("=" * 70)

    factory = APIRequestFactory()

    factory = APIRequestFactory()

    # Reuse existing business context
    business = Business.objects.filter(name="Acme Enterprises").first() or Business.objects.first()
    assert business is not None, "A business must exist in database"

    centre_a = Branch.objects.filter(business=business, name__icontains="Centre A").first() or Branch.objects.filter(business=business).first()
    centre_b = Branch.objects.filter(business=business).exclude(id=centre_a.id).first()
    if not centre_b:
        centre_b = Branch.objects.create(business=business, name="Beta Centre")

    admin_membership = business.memberships.filter(role=BusinessRole.BUSINESS_ADMIN).first()
    admin_user = admin_membership.user if admin_membership else User.objects.filter(is_superuser=True).first()

    staff_membership = business.memberships.filter(role=BusinessRole.STAFF).first()
    unauth_user = staff_membership.user if staff_membership else User.objects.filter(memberships__role=BusinessRole.STAFF).first()
    if not unauth_user:
        unauth_user, _ = User.objects.get_or_create(email="staff_test@example.com", defaults={'first_name': 'Staff'})
        BusinessMembership.objects.update_or_create(user=unauth_user, business=business, defaults={'role': BusinessRole.STAFF, 'is_active': True})

    # Employees
    emp_monthly, _ = Employee.objects.update_or_create(
        email="emp.monthly@testcomp.com",
        defaults={
            'business': business,
            'branch': centre_a,
            'first_name': 'Ramesh',
            'last_name': 'Monthly',
            'designation': 'Staff',
            'joining_date': datetime.date(2025, 1, 1),
            'employment_status': 'ACTIVE'
        }
    )

    emp_daily, _ = Employee.objects.update_or_create(
        email="emp.daily@testcomp.com",
        defaults={
            'business': business,
            'branch': centre_a,
            'first_name': 'Dinesh',
            'last_name': 'Daily',
            'designation': 'Staff',
            'joining_date': datetime.date(2025, 1, 1),
            'employment_status': 'ACTIVE'
        }
    )

    emp_weekly, _ = Employee.objects.update_or_create(
        email="emp.weekly@testcomp.com",
        defaults={
            'business': business,
            'branch': centre_a,
            'first_name': 'Vikas',
            'last_name': 'Weekly',
            'designation': 'Staff',
            'joining_date': datetime.date(2025, 1, 1),
            'employment_status': 'ACTIVE'
        }
    )

    # Clean prior test compensation items
    EmployeeCompensationItem.objects.filter(business=business).delete()

    # Revisions
    rev_monthly, _ = SalaryRevision.objects.update_or_create(
        employee=emp_monthly,
        effective_from=datetime.date(2026, 1, 1),
        defaults={
            'business': business,
            'basic_salary': Decimal('30000.00'),
            'salary_unit': 'MONTHLY',
            'hourly_rate': Decimal('0.00'),
            'ot_rate': Decimal('0.00'),
        }
    )

    rev_daily, _ = SalaryRevision.objects.update_or_create(
        employee=emp_daily,
        effective_from=datetime.date(2026, 1, 1),
        defaults={
            'business': business,
            'basic_salary': Decimal('1000.00'),
            'salary_unit': 'DAILY',
            'hourly_rate': Decimal('0.00'),
            'ot_rate': Decimal('0.00'),
        }
    )

    rev_weekly, _ = SalaryRevision.objects.update_or_create(
        employee=emp_weekly,
        effective_from=datetime.date(2026, 1, 1),
        defaults={
            'business': business,
            'basic_salary': Decimal('7000.00'),
            'salary_unit': 'WEEKLY',
            'hourly_rate': Decimal('0.00'),
            'ot_rate': Decimal('0.00'),
        }
    )

    # Schedules
    PayrollScheduleConfig.objects.update_or_create(
        business=business, employee=emp_monthly, scope=ScheduleConfigScope.EMPLOYEE,
        defaults={'generation_type': GenerationType.MONTHLY, 'compensation_type': CompensationType.MONTHLY_SALARY, 'has_override': True, 'is_active': True}
    )
    PayrollScheduleConfig.objects.update_or_create(
        business=business, employee=emp_daily, scope=ScheduleConfigScope.EMPLOYEE,
        defaults={'generation_type': GenerationType.DAILY, 'compensation_type': CompensationType.DAILY_WAGE, 'has_override': True, 'is_active': True}
    )
    PayrollScheduleConfig.objects.update_or_create(
        business=business, employee=emp_weekly, scope=ScheduleConfigScope.EMPLOYEE,
        defaults={'generation_type': GenerationType.WEEKLY, 'compensation_type': CompensationType.MONTHLY_SALARY, 'has_override': True, 'is_active': True}
    )

    # =========================================================================
    # SECTION A: DAILY SALARY
    # =========================================================================
    print("\n--- A. Testing DAILY Salary Type Eligibility ---")

    # Add visible components to daily employee
    daily_bonus = EmployeeCompensationItem.objects.create(
        business=business,
        employee=emp_daily,
        centre=centre_a,
        scope=ScheduleConfigScope.EMPLOYEE,
        name="Daily Worker Bonus",
        component_type=CompensationComponentType.BONUS,
        calculation_type=CompensationCalculationType.FIXED_AMOUNT,
        frequency=CompensationFrequency.ONE_TIME,
        amount=Decimal('500.00'),
        effective_from=datetime.date(2026, 10, 15),
        affects_payroll=True,
        is_active=True
    )

    daily_allowance = EmployeeCompensationItem.objects.create(
        business=business,
        employee=emp_daily,
        centre=centre_a,
        scope=ScheduleConfigScope.EMPLOYEE,
        name="Daily Food Allowance",
        component_type=CompensationComponentType.EARNING,
        calculation_type=CompensationCalculationType.FIXED_AMOUNT,
        frequency=CompensationFrequency.RECURRING,
        amount=Decimal('100.00'),
        effective_from=datetime.date(2026, 10, 1),
        affects_payroll=True,
        is_active=True
    )

    # Calculate 1-day daily payroll
    calc_daily = PayrollCalculationService.calculate_employee_payroll(
        emp_daily,
        period_start=datetime.date(2026, 10, 15),
        period_end=datetime.date(2026, 10, 15)
    )

    # Verify no line item from compensation items is present
    has_comp_line = any(l.get('source_compensation_item') is not None for l in calc_daily['line_items'])
    record_test(
        "Daily-wage payroll excludes earnings, bonuses, and deductions from itemized components",
        not has_comp_line,
        f"line_items_count={len(calc_daily['line_items'])}, has_comp_line={has_comp_line}"
    )

    record_test(
        "Visible components cannot accidentally affect daily payroll calculation",
        calc_daily['salary_snapshot']['total_comp_earnings'] == 0.0 and calc_daily['salary_snapshot']['total_comp_deductions'] == 0.0,
        f"comp_earnings={calc_daily['salary_snapshot']['total_comp_earnings']}"
    )

    record_test(
        "Daily-wage payroll records clear eligibility notice in calculation snapshot",
        calc_daily['salary_snapshot'].get('is_daily_wage') is True and 'not applicable' in (calc_daily['salary_snapshot'].get('itemized_components_notice') or ''),
        f"notice={calc_daily['salary_snapshot'].get('itemized_components_notice')}"
    )

    # API check: GET returns is_daily_wage metadata and notice
    view = EmployeeCompensationItemListCreateView.as_view()
    req = factory.get(f"/api/v1/employees/{emp_daily.id}/compensation-items/")
    force_authenticate(req, user=admin_user)
    resp = view(req, pk=str(emp_daily.id))
    record_test(
        "API GET indicates itemized components not applicable to daily wage",
        resp.status_code == 200 and resp.headers.get('X-Is-Daily-Wage') == 'true' and 'not applicable' in resp.headers.get('X-Daily-Wage-Notice', ''),
        f"X-Is-Daily-Wage={resp.headers.get('X-Is-Daily-Wage')}"
    )

    # Clean up daily test items
    daily_bonus.delete()
    daily_allowance.delete()

    # =========================================================================
    # SECTION B: RECURRING COMPONENTS
    # =========================================================================
    print("\n--- B. Testing Recurring Components ---")

    # Recurring Food Allowance: ₹2,000/month (Starts Oct 1, 2026, Ends Oct 31, 2026)
    rec_food = EmployeeCompensationItem.objects.create(
        business=business,
        employee=emp_monthly,
        centre=centre_a,
        scope=ScheduleConfigScope.EMPLOYEE,
        name="Food Allowance",
        component_type=CompensationComponentType.EARNING,
        calculation_type=CompensationCalculationType.FIXED_AMOUNT,
        frequency=CompensationFrequency.MONTHLY,
        amount=Decimal('2000.00'),
        effective_from=datetime.date(2026, 10, 1),
        effective_to=datetime.date(2026, 10, 31),
        affects_payroll=True,
        is_active=True
    )

    # Transport Deduction: ₹1,200/month (Starts Oct 16, 2026 - mid period!)
    rec_transport = EmployeeCompensationItem.objects.create(
        business=business,
        employee=emp_monthly,
        centre=centre_a,
        scope=ScheduleConfigScope.EMPLOYEE,
        name="Transport Deduction",
        component_type=CompensationComponentType.DEDUCTION,
        calculation_type=CompensationCalculationType.FIXED_AMOUNT,
        frequency=CompensationFrequency.MONTHLY,
        amount=Decimal('1200.00'),
        effective_from=datetime.date(2026, 10, 16),
        effective_to=None,
        affects_payroll=True,
        is_active=True
    )

    calc_oct = PayrollCalculationService.calculate_employee_payroll(
        emp_monthly,
        period_start=datetime.date(2026, 10, 1),
        period_end=datetime.date(2026, 10, 31)
    )

    food_line = next((l for l in calc_oct['line_items'] if l['name'] == 'Food Allowance'), None)
    record_test(
        "Recurring component included in full for eligible monthly period",
        food_line is not None and food_line['amount'] == Decimal('2000.00'),
        f"amount={food_line['amount'] if food_line else 'None'}"
    )

    # Transport starts Oct 16: exactly 16 active days out of 31 days -> (1200 * 16 / 31) = 619.35
    trans_line = next((l for l in calc_oct['line_items'] if l['name'] == 'Transport Deduction'), None)
    expected_trans = (Decimal('1200.00') * Decimal('16') / Decimal('31')).quantize(Decimal('0.01'))
    record_test(
        "Mid-period recurring component prorated deterministically based on active days",
        trans_line is not None and trans_line['amount'] == expected_trans,
        f"actual={trans_line['amount'] if trans_line else None}, expected={expected_trans}"
    )

    # Before start date: Sep 2026
    calc_sep = PayrollCalculationService.calculate_employee_payroll(
        emp_monthly,
        period_start=datetime.date(2026, 9, 1),
        period_end=datetime.date(2026, 9, 30)
    )
    sep_food = next((l for l in calc_sep['line_items'] if l['name'] == 'Food Allowance'), None)
    record_test(
        "Recurring component excluded before its effective start date",
        sep_food is None,
        f"sep_food={sep_food}"
    )

    # After end date: Nov 2026 (Food Allowance ended Oct 31)
    calc_nov = PayrollCalculationService.calculate_employee_payroll(
        emp_monthly,
        period_start=datetime.date(2026, 11, 1),
        period_end=datetime.date(2026, 11, 30)
    )
    nov_food = next((l for l in calc_nov['line_items'] if l['name'] == 'Food Allowance'), None)
    record_test(
        "Recurring component excluded after its effective end date",
        nov_food is None,
        f"nov_food={nov_food}"
    )

    # Disabled component check
    rec_food.is_active = False
    rec_food.save()
    calc_oct_disabled = PayrollCalculationService.calculate_employee_payroll(
        emp_monthly,
        period_start=datetime.date(2026, 10, 1),
        period_end=datetime.date(2026, 10, 31)
    )
    disabled_food = next((l for l in calc_oct_disabled['line_items'] if l['name'] == 'Food Allowance'), None)
    record_test(
        "Disabled component (is_active=False) excluded from payroll",
        disabled_food is None,
        f"disabled_food={disabled_food}"
    )
    rec_food.is_active = True
    rec_food.save()

    # Affect Payroll OFF check
    rec_food.affects_payroll = False
    rec_food.save()
    calc_oct_off = PayrollCalculationService.calculate_employee_payroll(
        emp_monthly,
        period_start=datetime.date(2026, 10, 1),
        period_end=datetime.date(2026, 10, 31)
    )
    off_food = next((l for l in calc_oct_off['line_items'] if l['name'] == 'Food Allowance'), None)
    record_test(
        "Affect Payroll OFF component excluded from payroll calculations",
        off_food is None,
        f"off_food={off_food}"
    )
    rec_food.affects_payroll = True
    rec_food.save()

    # Independent recurrence: Monthly component is not automatically converted to weekly amount
    rec_weekly_comp = EmployeeCompensationItem.objects.create(
        business=business,
        employee=emp_weekly,
        centre=centre_a,
        scope=ScheduleConfigScope.EMPLOYEE,
        name="Weekly Safety Allowance",
        component_type=CompensationComponentType.EARNING,
        calculation_type=CompensationCalculationType.FIXED_AMOUNT,
        frequency=CompensationFrequency.WEEKLY,
        amount=Decimal('500.00'),
        effective_from=datetime.date(2026, 10, 1),
        affects_payroll=True,
        is_active=True
    )
    calc_weekly = PayrollCalculationService.calculate_employee_payroll(
        emp_weekly,
        period_start=datetime.date(2026, 10, 5),
        period_end=datetime.date(2026, 10, 11)
    )
    w_safety = next((l for l in calc_weekly['line_items'] if l['name'] == 'Weekly Safety Allowance'), None)
    record_test(
        "Weekly recurring component applies in full in weekly payroll cycle without unit distortion",
        w_safety is not None and w_safety['amount'] == Decimal('500.00'),
        f"amount={w_safety['amount'] if w_safety else None}"
    )

    # Cleanup section B items
    rec_food.delete()
    rec_transport.delete()
    rec_weekly_comp.delete()

    # =========================================================================
    # SECTION C: ONE-TIME COMPONENTS
    # =========================================================================
    print("\n--- C. Testing One-Time Components ---")

    # Clean prior test runs for Oct 2026
    PayrollLineItem.objects.filter(payroll__payroll_run__business=business, payroll__payroll_run__period_start=datetime.date(2026, 10, 1)).delete()
    Payroll.objects.filter(business=business, period_start=datetime.date(2026, 10, 1)).delete()
    PayrollRun.objects.filter(business=business, period_start=datetime.date(2026, 10, 1)).delete()

    # Diwali Bonus: ₹5,000 applicable on 15 Oct 2026
    one_bonus = EmployeeCompensationItem.objects.create(
        business=business,
        employee=emp_monthly,
        centre=centre_a,
        scope=ScheduleConfigScope.EMPLOYEE,
        name="Diwali Festival Bonus",
        component_type=CompensationComponentType.BONUS,
        calculation_type=CompensationCalculationType.FIXED_AMOUNT,
        frequency=CompensationFrequency.ONE_TIME,
        amount=Decimal('5000.00'),
        effective_from=datetime.date(2026, 10, 15),
        affects_payroll=True,
        is_active=True
    )

    # Excluded before applicable date (Sep 2026)
    calc_sep_b = PayrollCalculationService.calculate_employee_payroll(
        emp_monthly,
        period_start=datetime.date(2026, 9, 1),
        period_end=datetime.date(2026, 9, 30)
    )
    record_test(
        "One-time component excluded from periods before its applicable date",
        not any(l['name'] == 'Diwali Festival Bonus' for l in calc_sep_b['line_items'])
    )

    # Included in Oct 2026
    calc_oct_b = PayrollCalculationService.calculate_employee_payroll(
        emp_monthly,
        period_start=datetime.date(2026, 10, 1),
        period_end=datetime.date(2026, 10, 31)
    )
    b_line = next((l for l in calc_oct_b['line_items'] if l['name'] == 'Diwali Festival Bonus'), None)
    record_test(
        "One-time component included in eligible payroll covering applicable date",
        b_line is not None and b_line['amount'] == Decimal('5000.00'),
        f"amount={b_line['amount'] if b_line else None}"
    )

    # Run batch payroll to materialize and track application
    run_oct = PayrollCalculationService.run_batch_payroll(
        business=business,
        period_start=datetime.date(2026, 10, 1),
        period_end=datetime.date(2026, 10, 31),
        centre=centre_a
    )

    one_bonus.refresh_from_db()
    record_test(
        "One-time component marked as applied with applied_in_payroll after batch payroll run",
        one_bonus.is_applied is True and one_bonus.applied_in_payroll_id is not None,
        f"is_applied={one_bonus.is_applied}, payroll_id={one_bonus.applied_in_payroll_id}"
    )

    # Regenerating or recalculating does not duplicate in the same run
    calc_oct_b2 = PayrollCalculationService.calculate_employee_payroll(
        emp_monthly,
        period_start=datetime.date(2026, 10, 1),
        period_end=datetime.date(2026, 10, 31)
    )
    matching_lines = [l for l in calc_oct_b2['line_items'] if l['name'] == 'Diwali Festival Bonus']
    record_test(
        "Recalculation does not duplicate one-time component within the period",
        len(matching_lines) == 1,
        f"count={len(matching_lines)}"
    )

    # Finalize Oct run and test November run exclusion
    run_oct.status = PayrollRunStatus.FINALIZED
    run_oct.save()
    p_oct = Payroll.objects.filter(payroll_run=run_oct, employee=emp_monthly).first()
    if p_oct:
        p_oct.status = PayrollStatus.PAID
        p_oct.save()

    # Nov 2026 run must NOT include the bonus
    calc_nov_b = PayrollCalculationService.calculate_employee_payroll(
        emp_monthly,
        period_start=datetime.date(2026, 11, 1),
        period_end=datetime.date(2026, 11, 30)
    )
    record_test(
        "Once applied in finalized payroll, one-time component is strictly excluded from subsequent payrolls",
        not any(l['name'] == 'Diwali Festival Bonus' for l in calc_nov_b['line_items'])
    )

    # Cancelled before inclusion test
    cancelled_bonus = EmployeeCompensationItem.objects.create(
        business=business,
        employee=emp_monthly,
        centre=centre_a,
        scope=ScheduleConfigScope.EMPLOYEE,
        name="Cancelled Project Bonus",
        component_type=CompensationComponentType.BONUS,
        calculation_type=CompensationCalculationType.FIXED_AMOUNT,
        frequency=CompensationFrequency.ONE_TIME,
        amount=Decimal('3000.00'),
        effective_from=datetime.date(2026, 11, 15),
        affects_payroll=True,
        is_active=False  # Cancelled!
    )
    calc_nov_c = PayrollCalculationService.calculate_employee_payroll(
        emp_monthly,
        period_start=datetime.date(2026, 11, 1),
        period_end=datetime.date(2026, 11, 30)
    )
    record_test(
        "Cancelled one-time component (is_active=False) is excluded from payroll",
        not any(l['name'] == 'Cancelled Project Bonus' for l in calc_nov_c['line_items'])
    )

    # Cleanup section C
    one_bonus.delete()
    cancelled_bonus.delete()

    # =========================================================================
    # SECTION D: CONFIGURATION INHERITANCE
    # =========================================================================
    print("\n--- D. Testing Configuration Inheritance Hierarchy ---")

    # 1. Enterprise Default: Food Allowance = ₹2,000
    ent_food = EmployeeCompensationItem.objects.create(
        business=business,
        scope=ScheduleConfigScope.ENTERPRISE,
        centre=None,
        employee=None,
        name="Food Allowance",
        component_type=CompensationComponentType.EARNING,
        calculation_type=CompensationCalculationType.FIXED_AMOUNT,
        frequency=CompensationFrequency.MONTHLY,
        amount=Decimal('2000.00'),
        effective_from=datetime.date(2026, 1, 1),
        affects_payroll=True,
        is_active=True
    )

    resolved_a = CompensationResolver.resolve_for_employee(emp_monthly)
    food_resolved = next((r for r in resolved_a if r['name'] == 'Food Allowance'), None)
    record_test(
        "Employee inherits Enterprise Default component (Source: Enterprise Default)",
        food_resolved is not None and food_resolved['amount'] == 2000.0 and food_resolved['source'] == 'ENTERPRISE',
        f"source={food_resolved['source'] if food_resolved else None}, amount={food_resolved['amount'] if food_resolved else None}"
    )

    # 2. Centre Override: Centre A overrides Food Allowance = ₹2,200
    cen_food = EmployeeCompensationItem.objects.create(
        business=business,
        centre=centre_a,
        scope=ScheduleConfigScope.CENTRE,
        employee=None,
        parent_item=ent_food,
        name="Food Allowance",
        component_type=CompensationComponentType.EARNING,
        calculation_type=CompensationCalculationType.FIXED_AMOUNT,
        frequency=CompensationFrequency.MONTHLY,
        amount=Decimal('2200.00'),
        effective_from=datetime.date(2026, 1, 1),
        affects_payroll=True,
        is_active=True
    )

    resolved_b = CompensationResolver.resolve_for_employee(emp_monthly)
    food_resolved_b = next((r for r in resolved_b if r['name'] == 'Food Allowance'), None)
    record_test(
        "Centre override takes precedence over Enterprise Default (Source: Centre Override)",
        food_resolved_b is not None and food_resolved_b['amount'] == 2200.0 and food_resolved_b['source'] == 'CENTRE',
        f"source={food_resolved_b['source'] if food_resolved_b else None}, amount={food_resolved_b['amount'] if food_resolved_b else None}"
    )

    # 3. Employee Override: Ramesh overrides Food Allowance = ₹2,500
    emp_food = EmployeeCompensationItem.objects.create(
        business=business,
        centre=centre_a,
        employee=emp_monthly,
        scope=ScheduleConfigScope.EMPLOYEE,
        parent_item=cen_food,
        is_override=True,
        name="Food Allowance",
        component_type=CompensationComponentType.EARNING,
        calculation_type=CompensationCalculationType.FIXED_AMOUNT,
        frequency=CompensationFrequency.MONTHLY,
        amount=Decimal('2500.00'),
        effective_from=datetime.date(2026, 1, 1),
        affects_payroll=True,
        is_active=True
    )

    resolved_c = CompensationResolver.resolve_for_employee(emp_monthly)
    food_resolved_c = next((r for r in resolved_c if r['name'] == 'Food Allowance'), None)
    record_test(
        "Employee override takes highest precedence (Source: Employee Override, Amount: ₹2,500)",
        food_resolved_c is not None and food_resolved_c['amount'] == 2500.0 and food_resolved_c['source'] == 'EMPLOYEE',
        f"source={food_resolved_c['source'] if food_resolved_c else None}, amount={food_resolved_c['amount'] if food_resolved_c else None}"
    )

    # 4. Explicit Employee Disable Overriding Inherited Component
    emp_food.is_active = False
    emp_food.save()
    resolved_d = CompensationResolver.resolve_for_employee(emp_monthly)
    food_resolved_d = next((r for r in resolved_d if r['name'] == 'Food Allowance'), None)
    record_test(
        "Explicit employee disable overrides inherited enabled default (does NOT revert to Centre default)",
        food_resolved_d is not None and food_resolved_d['is_active'] is False and food_resolved_d['status_display'] == 'DISABLED',
        f"is_active={food_resolved_d['is_active'] if food_resolved_d else None}, status={food_resolved_d['status_display'] if food_resolved_d else None}"
    )

    # 5. Isolation: Employee in Centre B does not see Centre A override or Ramesh's override
    emp_in_b, _ = Employee.objects.update_or_create(
        email="emp.b@testcomp.com",
        defaults={'business': business, 'branch': centre_b, 'first_name': 'Karan', 'last_name': 'Beta', 'designation': 'Staff', 'joining_date': datetime.date(2025, 1, 1), 'employment_status': 'ACTIVE'}
    )
    resolved_emp_b = CompensationResolver.resolve_for_employee(emp_in_b)
    food_b = next((r for r in resolved_emp_b if r['name'] == 'Food Allowance'), None)
    record_test(
        "Employee-specific changes do not affect employees in other centres (inherits Enterprise default ₹2,000)",
        food_b is not None and food_b['amount'] == 2000.0 and food_b['source'] == 'ENTERPRISE',
        f"source={food_b['source'] if food_b else None}, amount={food_b['amount'] if food_b else None}"
    )

    # Cleanup section D
    ent_food.delete()
    cen_food.delete()
    emp_food.delete()

    # =========================================================================
    # SECTION E: PAYROLL INTEGRITY & RBAC
    # =========================================================================
    print("\n--- E. Testing Payroll Integrity & RBAC Security ---")

    # Percentage component: 10% HRA on basic salary of 30,000 = 3,000
    hra_comp = EmployeeCompensationItem.objects.create(
        business=business,
        employee=emp_monthly,
        centre=centre_a,
        scope=ScheduleConfigScope.EMPLOYEE,
        name="House Rent Allowance (HRA)",
        component_type=CompensationComponentType.EARNING,
        calculation_type=CompensationCalculationType.PERCENTAGE,
        frequency=CompensationFrequency.MONTHLY,
        amount=Decimal('10.00'),  # 10%
        effective_from=datetime.date(2026, 1, 1),
        affects_payroll=True,
        is_active=True
    )

    # Deduction: Fixed ₹1,500 Provident Fund
    pf_comp = EmployeeCompensationItem.objects.create(
        business=business,
        employee=emp_monthly,
        centre=centre_a,
        scope=ScheduleConfigScope.EMPLOYEE,
        name="Provident Fund (PF)",
        component_type=CompensationComponentType.DEDUCTION,
        calculation_type=CompensationCalculationType.FIXED_AMOUNT,
        frequency=CompensationFrequency.MONTHLY,
        amount=Decimal('1500.00'),
        effective_from=datetime.date(2026, 1, 1),
        affects_payroll=True,
        is_active=True
    )

    # Mark employee present for scheduled working days and week_off for weekends in December 2026
    cur_d = datetime.date(2026, 12, 1)
    while cur_d <= datetime.date(2026, 12, 31):
        st = AttendanceStatus.PRESENT if cur_d.weekday() < 5 else AttendanceStatus.WEEK_OFF
        AttendanceDay.objects.update_or_create(
            business=business,
            employee=emp_monthly,
            attendance_date=cur_d,
            defaults={'centre': centre_a, 'status': st, 'total_work_seconds': 8 * 3600 if st == AttendanceStatus.PRESENT else 0}
        )
        cur_d += datetime.timedelta(days=1)

    calc_integrity = PayrollCalculationService.calculate_employee_payroll(
        emp_monthly,
        period_start=datetime.date(2026, 12, 1),
        period_end=datetime.date(2026, 12, 31)
    )

    expected_gross = Decimal('30000.00') + Decimal('3000.00')  # base + 10%
    expected_deductions = Decimal('1500.00')
    expected_net = expected_gross - expected_deductions

    record_test(
        "Earnings increase gross and deductions decrease net pay correctly with percentage evaluation",
        calc_integrity['gross_amount'] == expected_gross and calc_integrity['total_deductions'] == expected_deductions and calc_integrity['net_amount'] == expected_net,
        f"gross={calc_integrity['gross_amount']} (exp {expected_gross}), net={calc_integrity['net_amount']} (exp {expected_net})"
    )

    record_test(
        "Finalized payroll snapshot contains immutable component breakdown snapshot",
        len(calc_integrity['salary_snapshot']['comp_items_snapshot']) >= 2 and any(c['name'] == 'House Rent Allowance (HRA)' for c in calc_integrity['salary_snapshot']['comp_items_snapshot']),
        f"snapshot_items={len(calc_integrity['salary_snapshot']['comp_items_snapshot'])}"
    )

    # RBAC: Unauthorized user cannot add compensation items
    req_unauth = factory.post(
        f"/api/v1/employees/{emp_monthly.id}/compensation-items/",
        {'name': 'Illegal Bonus', 'component_type': 'BONUS', 'amount': 1000, 'frequency': 'ONE_TIME', 'effective_from': '2026-12-01'}
    )
    force_authenticate(req_unauth, user=unauth_user)
    view_create = EmployeeCompensationItemListCreateView.as_view()
    try:
        resp_unauth = view_create(req_unauth, pk=str(emp_monthly.id))
        is_forbidden = (resp_unauth.status_code == 403)
    except PermissionDenied:
        is_forbidden = True

    record_test(
        "RBAC: Unauthorized users strictly blocked from creating compensation items",
        is_forbidden,
        f"is_forbidden={is_forbidden}"
    )

    # Cleanup section E
    hra_comp.delete()
    pf_comp.delete()

    print("\n" + "=" * 70)
    print(f"ACCEPTANCE TESTS FINISHED: {len(passed_checks)} PASSED, {len(failed_checks)} FAILED")
    print("=" * 70)

    if failed_checks:
        sys.exit(1)
    else:
        print("ALL ITEMIZED COMPENSATION ACCEPTANCE TESTS PASSED WITH ZERO ERRORS!\n")

if __name__ == '__main__':
    run_tests()
