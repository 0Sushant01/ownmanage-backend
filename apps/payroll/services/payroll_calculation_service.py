from decimal import Decimal
from typing import Dict, Any, List, Optional
from datetime import date
from django.db import transaction
from django.utils import timezone

from apps.payroll.models import (
    Payroll, PayrollRun, PayrollRunStatus, PayrollStatus, SalaryRevision, SalaryStructure
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
        Formula: Base Salary + Allowances + Overtime - Unpaid Leaves - Deductions = Net Pay
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
        allowances = {}
        deduction_rules = {}

        if revision:
            basic_salary = Decimal(str(revision.basic_salary))
            currency = revision.currency
            hourly_rate = Decimal(str(revision.hourly_rate))
            ot_rate = Decimal(str(revision.ot_rate))
            allowances = revision.allowances or {}
            deduction_rules = revision.deduction_rules or {}
        else:
            # Fallback to legacy SalaryStructure
            legacy = SalaryStructure.objects.filter(
                employee=employee,
                effective_from__lte=period_end
            ).order_by('-effective_from').first()

            if legacy:
                basic_salary = Decimal(str(legacy.basic_salary))
                currency = legacy.currency
                for comp in legacy.components.all():
                    if comp.component_type == 'EARNING':
                        allowances[comp.name] = float(comp.amount)
                    else:
                        deduction_rules[comp.name] = float(comp.amount)

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

        # Total ot seconds
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
            # Calculate overlapping days
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

        # Calculate daily rate for unpaid deductions
        daily_rate = (basic_salary / Decimal(str(effective_working_days))) if effective_working_days > 0 else Decimal('0.00')

        # Allowances total
        total_allowances = Decimal('0.00')
        for k, v in allowances.items():
            total_allowances += Decimal(str(v))

        # Overtime pay
        if ot_rate > 0:
            ot_amount = ot_hours * ot_rate
        elif hourly_rate > 0:
            ot_amount = ot_hours * (hourly_rate * Decimal('1.5'))
        else:
            ot_amount = Decimal('0.00')

        gross_amount = basic_salary + total_allowances + ot_amount

        # Deductions
        unpaid_deduction = unpaid_leave_days * daily_rate
        total_deductions = unpaid_deduction
        for k, v in deduction_rules.items():
            total_deductions += Decimal(str(v))

        net_amount = max(Decimal('0.00'), gross_amount - total_deductions)

        paid_days_effective = Decimal(str(present_count + weekly_off_count + holiday_count)) + (Decimal(str(half_day_count)) * Decimal('0.5')) + paid_leave_days

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
            'salary_snapshot': {
                'basic_salary': float(basic_salary),
                'allowances': allowances,
                'deduction_rules': deduction_rules,
                'ot_amount': float(ot_amount),
                'unpaid_deduction': float(unpaid_deduction),
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
        Creates or updates items in a single atomic database transaction.
        """
        with transaction.atomic():
            run, _ = PayrollRun.objects.get_or_create(
                business=business,
                centre=centre,
                period_start=period_start,
                period_end=period_end,
                defaults={
                    'status': PayrollRunStatus.CALCULATING,
                    'approved_by': approved_by
                }
            )
            run.status = PayrollRunStatus.CALCULATING
            run.save(update_fields=['status', 'updated_at'])

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
                calc = cls.calculate_employee_payroll(emp, period_start, period_end)

                Payroll.objects.update_or_create(
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
                        'status': PayrollStatus.PROCESSED,
                    }
                )

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
