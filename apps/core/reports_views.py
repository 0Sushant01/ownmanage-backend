import csv
import calendar
from datetime import datetime, date, timedelta
from typing import Dict, Any, List
import zoneinfo

from django.http import HttpResponse
from django.utils import timezone
from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, NotFound, ValidationError

from apps.core.permissions import get_user_context
from apps.organization.models import Branch, Business, Employee, BusinessRole
from apps.attendance.models import AttendanceDay, AttendanceEventType, AttendanceStatus
from apps.leaves.models import LeaveRequest
from apps.organization.models import Holiday
from apps.payroll.models import Payroll, PayrollRun, PayrollLineItem
from apps.organization.services.policy_resolver import PolicyResolver
from apps.organization.services.permission_service import PermissionService


class AttendanceMonthlyReportView(views.APIView):
    """
    Monthly Attendance Summary Report.
    Returns aggregated attendance days, work hours, overtime, and leave stats per employee.
    Supports CSV export via ?format=csv or ?export=csv.
    """
    permission_classes = [IsAuthenticated]

    def perform_content_negotiation(self, request, force=False):
        if request.query_params.get('format') == 'csv' or request.query_params.get('export') == 'csv':
            from rest_framework.renderers import BaseRenderer
            class PassthroughRenderer(BaseRenderer):
                media_type = 'text/csv'
                format = 'csv'
                def render(self, data, accepted_media_type=None, renderer_context=None):
                    return data
            return (PassthroughRenderer(), 'text/csv')
        return super().perform_content_negotiation(request, force=force)

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        centre_id = request.query_params.get('centre_id') or request.query_params.get('branch_id')

        if ctx['is_superadmin']:
            biz_id = request.query_params.get('business_id')
            if biz_id:
                biz = Business.objects.filter(id=biz_id).first() or biz
            elif not biz and centre_id and centre_id not in ['all', 'ALL', 'null', '']:
                b = Branch.resolve_branch(centre_id)
                if b:
                    biz = b.business
            elif not biz:
                biz = Business.objects.filter(is_active=True).first()

        if not biz:
            raise PermissionDenied('No active business/enterprise context found.')

        # Permission check
        if not (ctx['is_superadmin'] or ctx['role'] == BusinessRole.BUSINESS_ADMIN):
            has_perm = PermissionService.has_permission(
                user=request.user,
                permission_key='reports.view',
                business=biz
            )
            if not has_perm:
                raise PermissionDenied('You do not have permission to view attendance reports.')

        # Month resolution
        month_str = request.query_params.get('month')
        now = timezone.now()
        if month_str:
            try:
                parts = month_str.split('-')
                year = int(parts[0])
                month = int(parts[1])
            except (ValueError, IndexError):
                year = now.year
                month = now.month
        else:
            year = now.year
            month = now.month

        num_days = calendar.monthrange(year, month)[1]
        start_date = date(year, month, 1)
        end_date = date(year, month, num_days)

        emp_qs = Employee.objects.filter(business=biz, employment_status='ACTIVE').select_related(
            'branch', 'department', 'user'
        )

        if centre_id and centre_id not in ['all', 'ALL', 'null', '']:
            branch_obj = Branch.resolve_branch(centre_id, business=biz)
            if branch_obj:
                emp_qs = emp_qs.filter(branch_id=branch_obj.id)
            else:
                emp_qs = emp_qs.none()

        # Center Manager scope limit
        if ctx['role'] == BusinessRole.MANAGER and ctx.get('employee') and ctx['employee'].branch_id:
            if not centre_id or centre_id in ['all', 'ALL']:
                emp_qs = emp_qs.filter(branch_id=ctx['employee'].branch_id)

        employees = list(emp_qs.order_by('branch__name', 'first_name', 'last_name'))

        # Fetch attendance days
        att_days = AttendanceDay.objects.filter(
            business=biz,
            attendance_date__gte=start_date,
            attendance_date__lte=end_date,
            employee__in=employees
        )
        days_by_emp: Dict[str, List[AttendanceDay]] = {}
        for d in att_days:
            days_by_emp.setdefault(str(d.employee_id), []).append(d)

        # Fetch approved leaves
        leaves = LeaveRequest.objects.filter(
            business=biz,
            start_date__lte=end_date,
            end_date__gte=start_date,
            status='APPROVED',
            employee__in=employees
        )
        leaves_by_emp: Dict[str, List[LeaveRequest]] = {}
        for l in leaves:
            leaves_by_emp.setdefault(str(l.employee_id), []).append(l)

        # Fetch holidays in month
        holidays = list(Holiday.objects.filter(
            business=biz,
            holiday_date__gte=start_date,
            holiday_date__lte=end_date
        ).prefetch_related('centres'))

        policies_cache = {}
        report_rows = []

        for emp in employees:
            centre = emp.branch
            centre_key = str(centre.id) if centre else 'default'
            if centre_key not in policies_cache:
                policies_cache[centre_key] = PolicyResolver.get_attendance_policy(centre=centre, business=biz)['effective']
            policy = policies_cache[centre_key]
            weekly_off_days = policy.get('weekly_off_days') or [policy.get('weekly_off', 6)]

            emp_days = days_by_emp.get(str(emp.id), [])
            emp_leaves = leaves_by_emp.get(str(emp.id), [])

            present_count = 0
            late_count = 0
            half_day_count = 0
            total_work_seconds = 0
            total_ot_seconds = 0

            days_map = {d.attendance_date: d for d in emp_days}

            for d in emp_days:
                if d.status in [AttendanceStatus.PRESENT, AttendanceStatus.OVERTIME]:
                    present_count += 1
                elif d.status == AttendanceStatus.HALF_DAY:
                    half_day_count += 1
                elif d.status == AttendanceStatus.LATE:
                    late_count += 1
                    present_count += 1

                total_work_seconds += d.total_work_seconds
                total_ot_seconds += d.overtime_seconds

            # Count leaves, holidays, weekly-offs, and absents across all calendar days
            leave_days_count = 0
            holiday_days_count = 0
            week_off_count = 0
            unmarked_days = 0

            cur = start_date
            while cur <= end_date:
                if cur in days_map:
                    cur += timedelta(days=1)
                    continue

                # Check approved leave
                is_leave = any(l.start_date <= cur <= l.end_date for l in emp_leaves)
                if is_leave:
                    leave_days_count += 1
                else:
                    # Check holiday
                    is_hol = any(
                        h.holiday_date == cur and (
                            h.applies_to_all_centres or
                            (centre and any(c.id == centre.id for c in h.centres.all()))
                        )
                        for h in holidays
                    )
                    if is_hol:
                        holiday_days_count += 1
                    elif cur.weekday() in weekly_off_days:
                        week_off_count += 1
                    else:
                        # Day has passed without punch or excuse
                        if cur < now.date():
                            unmarked_days += 1

                cur += timedelta(days=1)

            work_hours = round(total_work_seconds / 3600.0, 1)
            ot_hours = round(total_ot_seconds / 3600.0, 1)

            row = {
                'employee_id': str(emp.id),
                'employee_code': emp.employee_id or '—',
                'employee_name': emp.full_name,
                'centre_id': str(centre.id) if centre else None,
                'centre_name': centre.name if centre else '—',
                'department_name': emp.department.name if emp.department else '—',
                'days_in_month': num_days,
                'present_days': present_count,
                'half_days': half_day_count,
                'late_days': late_count,
                'leave_days': leave_days_count,
                'holiday_days': holiday_days_count,
                'weekly_off_days': week_off_count,
                'absent_unmarked_days': unmarked_days,
                'total_work_hours': work_hours,
                'total_overtime_hours': ot_hours,
            }
            report_rows.append(row)

        is_csv = request.query_params.get('format') == 'csv' or request.query_params.get('export') == 'csv'
        if is_csv:
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = f'attachment; filename="attendance_report_{year}_{month:02d}.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'Employee Code', 'Employee Name', 'Centre', 'Department',
                'Total Days', 'Present Days', 'Half Days', 'Late Days',
                'Leaves', 'Holidays', 'Weekly Offs', 'Absent/Unmarked',
                'Total Work Hours', 'Overtime Hours'
            ])
            for r in report_rows:
                writer.writerow([
                    r['employee_code'], r['employee_name'], r['centre_name'], r['department_name'],
                    r['days_in_month'], r['present_days'], r['half_days'], r['late_days'],
                    r['leave_days'], r['holiday_days'], r['weekly_off_days'], r['absent_unmarked_days'],
                    r['total_work_hours'], r['total_overtime_hours']
                ])
            return response

        return Response({
            'month': f"{year}-{month:02d}",
            'total_employees': len(employees),
            'records': report_rows
        })


class PayrollRegisterReportView(views.APIView):
    """
    Detailed Payroll Register Report.
    Listing itemized gross, deductions, and net disbursements formatted for accounting and banking.
    Supports CSV export via ?format=csv or ?export=csv.
    """
    permission_classes = [IsAuthenticated]

    def perform_content_negotiation(self, request, force=False):
        if request.query_params.get('format') == 'csv' or request.query_params.get('export') == 'csv':
            from rest_framework.renderers import BaseRenderer
            class PassthroughRenderer(BaseRenderer):
                media_type = 'text/csv'
                format = 'csv'
                def render(self, data, accepted_media_type=None, renderer_context=None):
                    return data
            return (PassthroughRenderer(), 'text/csv')
        return super().perform_content_negotiation(request, force=force)

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']

        if not (ctx['is_superadmin'] or ctx['role'] == BusinessRole.BUSINESS_ADMIN):
            raise PermissionDenied('Only Enterprise Administrators can export payroll registers.')

        qs = Payroll.objects.filter(business=biz).select_related(
            'employee', 'employee__branch', 'employee__department', 'payroll_run'
        ).prefetch_related('line_items').order_by('-period_start', 'employee__first_name')

        payroll_run_id = request.query_params.get('payroll_run_id')
        if payroll_run_id:
            qs = qs.filter(payroll_run_id=payroll_run_id)

        centre_id = request.query_params.get('centre_id')
        if centre_id and centre_id not in ['all', 'ALL', 'null', '']:
            branch_obj = Branch.resolve_branch(centre_id, business=biz)
            if branch_obj:
                qs = qs.filter(employee__branch_id=branch_obj.id)
            else:
                qs = qs.none()

        month_param = request.query_params.get('month') or request.query_params.get('period_month')
        period_start = request.query_params.get('period_start')

        if month_param and '-' in month_param:
            try:
                y, m = month_param.split('-')[:2]
                qs = qs.filter(period_start__year=int(y), period_start__month=int(m))
            except (ValueError, TypeError):
                pass
        elif period_start:
            if len(period_start) == 7 and '-' in period_start:
                try:
                    y, m = period_start.split('-')[:2]
                    qs = qs.filter(period_start__year=int(y), period_start__month=int(m))
                except (ValueError, TypeError):
                    pass
            else:
                qs = qs.filter(period_start__gte=period_start)

        status_param = request.query_params.get('status')
        if status_param and status_param not in ['all', 'ALL']:
            qs = qs.filter(status=status_param)

        records = []
        for p in qs:
            emp = p.employee
            centre = emp.branch if emp else None
            dept = emp.department if emp else None

            # Calculate itemized categories from line items
            allowance_total = 0.0
            bonus_total = 0.0
            ot_total = 0.0
            deduction_total = 0.0

            for li in p.line_items.all():
                amt = float(li.amount)
                if li.is_deduction:
                    deduction_total += amt
                elif li.line_type == 'BONUS':
                    bonus_total += amt
                elif li.line_type == 'OVERTIME':
                    ot_total += amt
                elif li.line_type == 'ALLOWANCE':
                    allowance_total += amt

            record = {
                'id': str(p.id),
                'employee_code': emp.employee_id if emp else '—',
                'employee_name': p.employee_name,
                'centre_name': centre.name if centre else '—',
                'department_name': dept.name if dept else '—',
                'period_start': str(p.period_start),
                'period_end': str(p.period_end),
                'currency': p.currency,
                'gross_amount': float(p.gross_amount),
                'allowances': allowance_total,
                'bonus': bonus_total,
                'overtime': ot_total,
                'total_deductions': float(p.total_deductions),
                'net_amount': float(p.net_amount),
                'status': p.status,
                'generated_at': p.generated_at.isoformat() if p.generated_at else str(p.created_at),
            }
            records.append(record)

        is_csv = request.query_params.get('format') == 'csv' or request.query_params.get('export') == 'csv'
        if is_csv:
            response = HttpResponse(content_type='text/csv; charset=utf-8')
            response['Content-Disposition'] = 'attachment; filename="payroll_register.csv"'
            writer = csv.writer(response)
            writer.writerow([
                'Employee Code', 'Employee Name', 'Centre', 'Department',
                'Period Start', 'Period End', 'Gross Amount', 'Allowances',
                'Bonuses', 'Overtime', 'Total Deductions', 'Net Payable',
                'Currency', 'Status'
            ])
            for r in records:
                writer.writerow([
                    r['employee_code'], r['employee_name'], r['centre_name'], r['department_name'],
                    r['period_start'], r['period_end'], r['gross_amount'], r['allowances'],
                    r['bonus'], r['overtime'], r['total_deductions'], r['net_amount'],
                    r['currency'], r['status']
                ])
            return response

        return Response({
            'count': len(records),
            'records': records
        })


class CentreComparisonReportView(views.APIView):
    """
    Multi-Centre Comparative Analytics Report.
    Compares active workforce, attendance compliance, overtime hours, and gross payroll across centres.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']

        if ctx['is_superadmin']:
            biz_id = request.query_params.get('business_id')
            if biz_id:
                biz = Business.objects.filter(id=biz_id).first() or biz
            elif not biz:
                biz = Business.objects.filter(is_active=True).first()

        if not biz:
            raise PermissionDenied('No active business context.')

        month_str = request.query_params.get('month')
        now = timezone.now()
        if month_str:
            try:
                parts = month_str.split('-')
                year = int(parts[0])
                month = int(parts[1])
            except (ValueError, IndexError):
                year = now.year
                month = now.month
        else:
            year = now.year
            month = now.month

        num_days = calendar.monthrange(year, month)[1]
        start_date = date(year, month, 1)
        end_date = date(year, month, num_days)

        centres = list(Branch.objects.filter(business=biz, is_active=True).order_by('name'))

        comparison_data = []

        for centre in centres:
            active_emps = Employee.objects.filter(business=biz, branch=centre, employment_status='ACTIVE').count()

            # Aggregate attendance stats
            att_qs = AttendanceDay.objects.filter(
                business=biz,
                centre=centre,
                attendance_date__gte=start_date,
                attendance_date__lte=end_date
            )
            total_present = att_qs.filter(status__in=[AttendanceStatus.PRESENT, AttendanceStatus.OVERTIME, AttendanceStatus.LATE]).count()
            total_work_sec = sum(att_qs.values_list('total_work_seconds', flat=True))
            total_ot_sec = sum(att_qs.values_list('overtime_seconds', flat=True))

            # Expected working days approximation
            expected_days = active_emps * 22
            attendance_rate = round((total_present / expected_days * 100.0), 1) if expected_days > 0 else 0.0

            # Aggregate payroll for this centre
            payrolls = Payroll.objects.filter(
                business=biz,
                employee__branch=centre,
                period_start__gte=start_date,
                period_end__lte=end_date
            )
            gross_total = sum(float(p.gross_amount) for p in payrolls)
            net_total = sum(float(p.net_amount) for p in payrolls)

            comparison_data.append({
                'centre_id': str(centre.id),
                'centre_name': centre.name,
                'city': centre.city or '',
                'active_employees': active_emps,
                'total_present_days': total_present,
                'attendance_compliance_pct': min(attendance_rate, 100.0),
                'total_work_hours': round(total_work_sec / 3600.0, 1),
                'total_overtime_hours': round(total_ot_sec / 3600.0, 1),
                'gross_payroll': gross_total,
                'net_payroll': net_total,
            })

        return Response({
            'month': f"{year}-{month:02d}",
            'centres_count': len(centres),
            'comparison': comparison_data
        })
