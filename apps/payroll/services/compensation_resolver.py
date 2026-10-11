from decimal import Decimal
from typing import Dict, Any, List, Optional
from datetime import date
from django.utils import timezone

from apps.payroll.models import (
    EmployeeCompensationItem, CompensationComponentType, CompensationCalculationType,
    CompensationFrequency, ScheduleConfigScope, SalaryRevision, PayrollStatus,
    PayrollLineItem
)
from apps.organization.models import Employee, Business, Branch
from apps.payroll.services.payroll_schedule_service import PayrollScheduleService


class CompensationResolver:
    """
    Service for resolving Itemized Compensation Components following the strict hierarchy:
    ENTERPRISE DEFAULT -> CENTRE OVERRIDE -> EMPLOYEE OVERRIDE / SPECIFIC.
    
    Enforces:
    1. Salary Type Eligibility: DAILY wage employees are strictly excluded from all itemized components.
    2. Affect Payroll ON/OFF toggle.
    3. One-Time vs Recurring rules and idempotent application tracking.
    4. Independent recurrence: Monthly components are never automatically converted into weekly amounts.
    5. Clean proration for components effective partway through a period.
    6. Exact configuration provenance (source, source_display, has_override).
    """

    @classmethod
    def resolve_for_employee(
        cls,
        employee: Employee,
        as_of_date: Optional[date] = None,
        include_inactive: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Resolves all effective compensation components for an employee.
        Consolidates Enterprise defaults, Centre overrides/defaults, and Employee overrides/specific components.
        If an employee explicitly disables an inherited component, the centre default does NOT re-enable it.
        """
        if as_of_date is None:
            as_of_date = timezone.localdate() if hasattr(timezone, 'localdate') else timezone.now().date()

        business = employee.business
        centre = employee.branch

        # 1. Fetch Enterprise defaults
        ent_items = EmployeeCompensationItem.objects.filter(
            business=business,
            scope=ScheduleConfigScope.ENTERPRISE
        ).select_related('created_by')

        # 2. Fetch Centre overrides / defaults
        cen_items = []
        if centre:
            cen_items = EmployeeCompensationItem.objects.filter(
                business=business,
                centre=centre,
                scope=ScheduleConfigScope.CENTRE
            ).select_related('created_by')

        # 3. Fetch Employee overrides and employee-specific components
        emp_items = EmployeeCompensationItem.objects.filter(
            employee=employee,
            scope=ScheduleConfigScope.EMPLOYEE
        ).select_related('created_by', 'applied_in_payroll')

        # Maps to resolve hierarchy
        # id_to_key maps item.id to its canonical resolution key
        id_to_key: Dict[Any, str] = {}
        resolved_map: Dict[str, Dict[str, Any]] = {}

        def get_canonical_key(item: EmployeeCompensationItem) -> str:
            # 1. If item has parent_item_id that we already mapped, use that key
            if item.parent_item_id and item.parent_item_id in id_to_key:
                return id_to_key[item.parent_item_id]
            # 2. Otherwise normalize by (name, component_type)
            return f"{item.name.strip().lower()}_{item.component_type.upper()}"

        # Step A: Load Enterprise defaults
        for item in ent_items:
            key = get_canonical_key(item)
            id_to_key[item.id] = key
            resolved_map[key] = {
                'item': item,
                'source': 'ENTERPRISE',
                'source_display': 'Enterprise Default',
                'has_override': False,
                'is_inherited': True,
                'parent_item_id': None,
                'effective_scope': 'ENTERPRISE',
            }

        # Step B: Apply Centre overrides / defaults
        for item in cen_items:
            key = get_canonical_key(item)
            id_to_key[item.id] = key
            has_parent = key in resolved_map
            parent_id = resolved_map[key]['item'].id if has_parent else (item.parent_item_id or None)
            resolved_map[key] = {
                'item': item,
                'source': 'CENTRE',
                'source_display': 'Centre Override' if (has_parent or item.is_override or item.parent_item_id) else 'Centre Default',
                'has_override': has_parent or bool(item.is_override or item.parent_item_id),
                'is_inherited': True,
                'parent_item_id': parent_id,
                'effective_scope': 'CENTRE',
            }

        # Step C: Apply Employee overrides and employee-specific components
        for item in emp_items:
            key = get_canonical_key(item)
            id_to_key[item.id] = key
            has_parent = key in resolved_map
            parent_id = resolved_map[key]['item'].id if has_parent else (item.parent_item_id or None)
            resolved_map[key] = {
                'item': item,
                'source': 'EMPLOYEE',
                'source_display': 'Employee Override' if (has_parent or item.is_override or item.parent_item_id) else 'Employee Specific',
                'has_override': has_parent or bool(item.is_override or item.parent_item_id),
                'is_inherited': False,
                'parent_item_id': parent_id,
                'effective_scope': 'EMPLOYEE',
            }

        # Build output list with status computation
        result = []
        for key, entry in resolved_map.items():
            item = entry['item']

            if not include_inactive and not item.is_active:
                continue

            status_display = cls.compute_status(item, as_of_date)

            result.append({
                'id': str(item.id),
                'name': item.name,
                'component_type': item.component_type,
                'calculation_type': item.calculation_type,
                'frequency': item.frequency,
                'recurrence_type': item.recurrence_type,
                'recurrence_frequency': item.recurrence_frequency,
                'amount': float(item.amount),
                'effective_from': item.effective_from,
                'effective_to': item.effective_to,
                'affects_payroll': item.affects_payroll,
                'is_active': item.is_active,
                'is_applied': item.is_applied,
                'applied_in_payroll_id': str(item.applied_in_payroll_id) if item.applied_in_payroll_id else None,
                'applied_at': item.applied_at.isoformat() if item.applied_at else None,
                'reason': item.reason,
                'notes': item.notes,
                'scope': item.scope,
                'source': entry['source'],
                'source_display': entry['source_display'],
                'has_override': entry['has_override'],
                'parent_item_id': str(entry['parent_item_id']) if entry['parent_item_id'] else None,
                'status_display': status_display,
                'created_by_name': item.created_by.get_full_name() if item.created_by else 'System Admin',
                'created_at': item.created_at.isoformat() if hasattr(item, 'created_at') and item.created_at else None,
                'instance': item,
            })

        # Sort: active first, then -effective_from, then name
        result.sort(key=lambda x: (not x['is_active'], str(x['effective_from']) if x['effective_from'] else '', x['name']))
        return result

    @classmethod
    def compute_status(cls, item: EmployeeCompensationItem, as_of_date: date) -> str:
        """
        Determines canonical, human-meaningful status string for a component.
        Labels: ACTIVE, UPCOMING, DISABLED, EXPIRED, CANCELLED, ALREADY_APPLIED.
        """
        if not item.is_active:
            return 'CANCELLED' if item.is_one_time else 'DISABLED'
        if item.is_one_time and item.is_applied:
            return 'ALREADY_APPLIED'
        if item.effective_from and item.effective_from > as_of_date:
            return 'UPCOMING'
        if item.effective_to and item.effective_to < as_of_date:
            return 'EXPIRED'
        return 'ACTIVE'

    @classmethod
    def is_daily_wage_employee(cls, employee: Employee) -> bool:
        """
        Returns True if employee's compensation type or salary unit is DAILY.
        """
        revision = SalaryRevision.objects.filter(
            employee=employee
        ).order_by('-effective_from').first()
        rev_unit = getattr(revision, 'salary_unit', None) if revision else None

        schedule_res = PayrollScheduleService.resolve_schedule(employee=employee)
        eff_cfg = schedule_res.get('effective_config', {})
        comp_type = eff_cfg.get('compensation_type', 'MONTHLY_SALARY')

        return (comp_type == 'DAILY_WAGE' or rev_unit == 'DAILY')

    @classmethod
    def get_eligible_components_for_payroll(
        cls,
        employee: Employee,
        period_start: date,
        period_end: date,
        basic_salary: Decimal = Decimal('0.00')
    ) -> List[Dict[str, Any]]:
        """
        Evaluates and calculates itemized compensation components strictly eligible for a given payroll period.
        
        Rules:
        1. If employee compensation type is DAILY: EXCLUDED. Returns empty list.
        2. If component is inactive (disabled/cancelled): EXCLUDED.
        3. If Affect Payroll is OFF (affects_payroll=False): EXCLUDED.
        4. Date range: item.effective_from <= period_end AND (effective_to is None or effective_to >= period_start).
        5. ONE_TIME components:
           - Applicable date (effective_from) must fall within [period_start, period_end].
           - Must not have already been applied in a finalized or paid payroll.
        6. RECURRING components:
           - Applied according to configured recurrence.
           - Independent recurrence: Monthly components are NEVER converted to weekly amounts automatically.
           - Proration: If component becomes effective or ends partway through the payroll period,
             prorate according to active days within the period.
        """
        # Rule 1: DAILY salary check
        if cls.is_daily_wage_employee(employee):
            return []

        resolved_items = cls.resolve_for_employee(employee, as_of_date=period_end, include_inactive=False)
        total_period_days = (period_end - period_start).days + 1
        eligible: List[Dict[str, Any]] = []

        for entry in resolved_items:
            item: EmployeeCompensationItem = entry['instance']

            # Rule 2: Enabled state
            if not item.is_active:
                continue

            # Rule 3: Affect Payroll setting
            if not item.affects_payroll:
                continue

            # Rule 4: Effective date boundaries
            if item.effective_from > period_end:
                # Starts after this period
                continue
            if item.effective_to and item.effective_to < period_start:
                # Ended before this period
                continue

            # Rule 5: ONE-TIME components
            if item.is_one_time:
                # Must fall within period
                if not (period_start <= item.effective_from <= period_end):
                    continue

                # Idempotency / duplicate protection: Check if already applied
                if item.is_applied and item.applied_in_payroll_id:
                    linked_payroll = getattr(item, 'applied_in_payroll', None)
                    # If applied in a DIFFERENT payroll period, strictly exclude from this period
                    if linked_payroll:
                        if linked_payroll.period_start != period_start or linked_payroll.period_end != period_end:
                            continue

                # Check if already included in a finalized/paid payroll line item of a different period
                already_in_paid_line = PayrollLineItem.objects.filter(
                    source_compensation_item=item,
                    payroll__status__in=[PayrollStatus.PAID]
                ).exclude(payroll__period_start=period_start, payroll__period_end=period_end).exists()
                if already_in_paid_line:
                    continue

                # One-time components apply the full configured amount
                if item.calculation_type == CompensationCalculationType.PERCENTAGE:
                    calc_amount = (basic_salary * (item.amount / Decimal('100.0'))).quantize(Decimal('0.01'))
                else:
                    calc_amount = item.amount.quantize(Decimal('0.01'))

                eligible.append({
                    'item': item,
                    'name': item.name,
                    'component_type': item.component_type,
                    'calculation_type': item.calculation_type,
                    'frequency': item.frequency,
                    'recurrence_type': 'ONE_TIME',
                    'recurrence_frequency': 'NONE',
                    'amount': item.amount,
                    'calculated_amount': calc_amount,
                    'is_prorated': False,
                    'active_days': total_period_days,
                    'total_period_days': total_period_days,
                    'source': entry['source'],
                    'source_display': entry['source_display'],
                    'affects_payroll': item.affects_payroll,
                    'is_deduction': (item.component_type == CompensationComponentType.DEDUCTION),
                })
                continue

            # Rule 6: RECURRING components
            # Independent recurrence: Monthly components are NOT converted to weekly amounts automatically.
            # Calculate base component amount
            if item.calculation_type == CompensationCalculationType.PERCENTAGE:
                base_amt = (basic_salary * (item.amount / Decimal('100.0'))).quantize(Decimal('0.01'))
            else:
                base_amt = item.amount.quantize(Decimal('0.01'))

            # Mid-period proration evaluation
            active_start = max(period_start, item.effective_from)
            active_end = min(period_end, item.effective_to) if item.effective_to else period_end
            active_days = max(0, (active_end - active_start).days + 1)

            if active_days < total_period_days and total_period_days > 0:
                is_prorated = True
                calc_amount = (base_amt * Decimal(str(active_days)) / Decimal(str(total_period_days))).quantize(Decimal('0.01'))
            else:
                is_prorated = False
                calc_amount = base_amt

            eligible.append({
                'item': item,
                'name': item.name,
                'component_type': item.component_type,
                'calculation_type': item.calculation_type,
                'frequency': item.frequency,
                'recurrence_type': 'RECURRING',
                'recurrence_frequency': item.recurrence_frequency,
                'amount': item.amount,
                'calculated_amount': calc_amount,
                'is_prorated': is_prorated,
                'active_days': active_days,
                'total_period_days': total_period_days,
                'source': entry['source'],
                'source_display': entry['source_display'],
                'affects_payroll': item.affects_payroll,
                'is_deduction': (item.component_type == CompensationComponentType.DEDUCTION),
            })

        return eligible
