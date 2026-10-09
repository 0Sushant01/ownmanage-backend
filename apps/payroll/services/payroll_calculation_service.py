from decimal import Decimal
from typing import Dict, Any, List, Optional
from datetime import date
from django.db import transaction
from django.utils import timezone

from apps.payroll.models import (
    Payroll, PayrollRun, PayrollRunStatus, PayrollStatus, SalaryRevision, SalaryStructure,
    PayrollLineItem, PayrollLineItemType, EmployeeCompensationItem,
    CompensationComponentType, CompensationCalculationType, CompensationFrequency
)
from apps.organization.models import Employee, Business, Branch
from apps.attendance.models import AttendanceDay, AttendanceStatus
from apps.leaves.models import LeaveRequest, LeaveRequestStatus


class PayrollCalculationService:
    @staticmethod
    def calculate_employee_payroll(
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

        if revision:
            basic_salary = Decimal(str(revision.basic_salary))
            currency = revision.currency
            hourly_rate = Decimal(str(revision.hourly_rate))
            ot_rate = Decimal(str(revision.ot_rate))
            legacy_allowances = revision.allowances or {}
            legacy_deductions = revision.deduction_rules or {}
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

        # 2. Compute attendance metrics for the period
        attendance_days = AttendanceDay.objects.filter(
            employee=employee,
            attendance_date__gte=period_start,
            attendance_date__lte=period_end
        )

        present_count = attendance_days.filter(
            status__in=[AttendanceStatus.PRESENT, AttendanceStatus.LATE, AttendanceStatus.OVERTIME]
        ).count()
        half_day_count = attendance_days.filter(status=AttendanceStatus.HALF_DAY).count()
        weekly_off_count = attendance_days.filter(status=AttendanceStatus.WEEK_OFF).count()
        holiday_count = attendance_days.filter(status=AttendanceStatus.HOLIDAY).count()

        total_ot_seconds = sum(day.overtime_seconds for day in attendance_days)
        ot_hours = Decimal(str(round(total_ot_seconds / 3600.0, 2)))

        # 3. Compute leave days
        approved_leaves = LeaveRequest.objects.filter(
            employee=employee,
            status=LeaveRequestStatus.APPROVED,
            start_date__lte=period_end,
            end_date__gte=period_start
        ).select_related('leave_type')

        paid_leave_days = Decimal('0.0')
        unpaid_leave_days = Decimal('0.0')

        for req in approved_leaves:
            l_start = max(req.start_date, period_start)
            l_end = min(req.end_date, period_end)
            days_count = Decimal(str((l_end - l_start).days + 1))
            if req.duration_type in ['FIRST_HALF', 'SECOND_HALF']:
                days_count = Decimal('0.5')

            if req.leave_type.is_paid:
                paid_leave_days += days_count
            else:
                unpaid_leave_days += days_count

        total_calendar_days = (period_end - period_start).days + 1
        effective_working_days = 30  # Standard payroll convention
        daily_rate = (basic_salary / Decimal(str(effective_working_days))).quantize(Decimal('0.01')) if effective_working_days > 0 else Decimal('0.00')

        paid_days_effective = Decimal(str(present_count + weekly_off_count + holiday_count)) + (Decimal(str(half_day_count)) * Decimal('0.5')) + paid_leave_days

        # Resolve effective payroll schedule & compensation type
        from apps.payroll.services.payroll_schedule_service import PayrollScheduleService
        schedule_res = PayrollScheduleService.resolve_schedule(employee=employee)
        eff_cfg = schedule_res['effective_config']
        comp_type = eff_cfg.get('compensation_type', 'MONTHLY_SALARY')

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
            skip_unpaid_penalty = True
        elif comp_type == 'HOURLY_WAGE':
            eff_hourly = hourly_rate if hourly_rate > 0 else (basic_salary / Decimal('240.00')).quantize(Decimal('0.01'))
            sum_work_sec = sum(getattr(d, 'total_work_seconds', 0) for d in attendance_days)
            hours_worked = Decimal(str(round(sum_work_sec / 3600.0, 2))) if sum_work_sec > 0 else (paid_days_effective * Decimal('8.00'))
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
            skip_unpaid_penalty = True
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
            skip_unpaid_penalty = False
        else:
            # MONTHLY_SALARY
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
            skip_unpaid_penalty = False

        # Overtime calculation
        if ot_rate > 0:
            ot_amount = ot_hours * ot_rate
        elif hourly_rate > 0:
            ot_amount = ot_hours * (hourly_rate * Decimal('1.5'))
        else:
            ot_amount = Decimal('0.00')

        if ot_amount > Decimal('0.00'):
            line_items_data.append({
                'name': f'Overtime ({ot_hours} hrs)',
                'line_type': PayrollLineItemType.OVERTIME,
                'amount': ot_amount,
                'rate': ot_rate if ot_rate > 0 else hourly_rate * Decimal('1.5'),
                'units': ot_hours,
                'is_deduction': False,
                'source_compensation_item': None,
            })

        # 4. Normalized EmployeeCompensationItem evaluation
        # Fetch active components applicable to this period
        comp_items = EmployeeCompensationItem.objects.filter(
            employee=employee,
            is_active=True,
            effective_from__lte=period_end
        )

        total_comp_earnings = Decimal('0.00')
        total_comp_deductions = Decimal('0.00')
        evaluated_comp_ids = set()

        for item in comp_items:
            # Check effective_to date
            if item.effective_to and item.effective_to < period_start:
                continue

            # ONE-TIME vs RECURRING check:
            # ONE_TIME applies strictly if effective_from is within [period_start, period_end]
            if item.frequency == CompensationFrequency.ONE_TIME:
                if not (period_start <= item.effective_from <= period_end):
                    continue

            evaluated_comp_ids.add(item.id)

            # Calculate amount
            if item.calculation_type == CompensationCalculationType.PERCENTAGE:
                comp_amt = (basic_salary * (item.amount / Decimal('100.0'))).quantize(Decimal('0.01'))
            else:
                comp_amt = item.amount

            is_ded = (item.component_type == CompensationComponentType.DEDUCTION)

            # Map line item type
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

        # 5. Legacy JSON fallback for any rules not migrated to EmployeeCompensationItem
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

        # Unpaid Leave penalty
        unpaid_deduction = Decimal('0.00') if skip_unpaid_penalty else (unpaid_leave_days * daily_rate).quantize(Decimal('0.01'))
        if unpaid_deduction > Decimal('0.00'):
            line_items_data.append({
                'name': f'Unpaid Leave ({unpaid_leave_days} days)',
                'line_type': PayrollLineItemType.UNPAID_LEAVE,
                'amount': unpaid_deduction,
                'rate': daily_rate,
                'units': unpaid_leave_days,
                'is_deduction': True,
                'source_compensation_item': None,
            })

        gross_amount = base_for_gross + total_comp_earnings + ot_amount
        total_deductions = total_comp_deductions + unpaid_deduction
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
            'salary_snapshot': {
                'basic_salary': float(basic_salary),
                'ot_amount': float(ot_amount),
                'unpaid_deduction': float(unpaid_deduction),
                'total_comp_earnings': float(total_comp_earnings),
                'total_comp_deductions': float(total_comp_deductions),
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
        approved_by=None
    ) -> PayrollRun:
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

        if existing_run and existing_run.status == PayrollRunStatus.FINALIZED:
            raise ValidationError('Cannot recalculate a FINALIZED payroll run. Finalized payroll records are immutable.')

        # Resolve effective schedule configuration for this batch scope
        batch_schedule = PayrollScheduleService.resolve_schedule(centre=centre, business=business)
        eff_cfg = batch_schedule['effective_config']
        expected_pay_date = PayrollScheduleService.calculate_expected_payment_date(period_end, eff_cfg)

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
                    'pay_frequency': eff_cfg.get('pay_frequency', 'MONTHLY_CALENDAR'),
                    'generation_mode': eff_cfg.get('generation_mode', 'MANUAL'),
                }
            )
            run.status = PayrollRunStatus.CALCULATING
            run.expected_payment_date = expected_pay_date
            run.pay_frequency = eff_cfg.get('pay_frequency', 'MONTHLY_CALENDAR')
            run.generation_mode = eff_cfg.get('generation_mode', 'MANUAL')
            run.save(update_fields=['status', 'expected_payment_date', 'pay_frequency', 'generation_mode', 'updated_at'])

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
                        'status': PayrollStatus.PROCESSED,
                    }
                )

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

                total_gross += calc['gross_amount']
                total_deductions += calc['total_deductions']
                total_net += calc['net_amount']

            run.total_employees = len(employees)
            run.total_gross = total_gross
            run.total_deductions = total_deductions
            run.total_net = total_net
            run.status = PayrollRunStatus.REVIEW
            run.save()

            return run

