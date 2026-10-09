from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, NotFound, ValidationError
from django.utils import timezone

from apps.core.permissions import get_user_context
from apps.organization.models import BusinessRole, Employee, Branch
from apps.organization.services.permission_service import PermissionService
from apps.payroll.models import ScheduleConfigScope
from apps.payroll.services.payroll_schedule_service import PayrollScheduleService


class EmployeePayrollScheduleView(views.APIView):
    """
    GET: Resolves effective payroll schedule for an employee (Hierarchy: Employee -> Centre -> Enterprise).
    POST: Creates or updates employee-specific custom schedule override.
    """
    permission_classes = [IsAuthenticated]

    def get_employee(self, request, pk):
        ctx = get_user_context(request)
        emp = Employee.objects.filter(id=pk).select_related('business', 'branch').first()
        if not emp:
            raise NotFound('Employee not found.')

        if not ctx['is_superadmin'] and emp.business_id != ctx['business'].id:
            raise PermissionDenied('Access forbidden.')

        return emp

    def check_can_manage(self, request, emp):
        ctx = get_user_context(request)
        if ctx['is_superadmin']:
            return True
        if ctx['role'] == BusinessRole.BUSINESS_ADMIN and emp.business_id == ctx['business'].id:
            return True
        if ctx['role'] == BusinessRole.MANAGER:
            return PermissionService.has_permission(request.user, 'salary.edit', business=emp.business, centre=emp.branch)
        return False

    def get(self, request, pk):
        emp = self.get_employee(request, pk)
        data = PayrollScheduleService.resolve_schedule(employee=emp)
        data['can_manage'] = self.check_can_manage(request, emp)
        return Response(data)

    def post(self, request, pk):
        emp = self.get_employee(request, pk)
        if not self.check_can_manage(request, emp):
            raise PermissionDenied('You do not have permission to configure employee payroll schedule overrides.')

        reason = request.data.get('change_reason') or 'Updated employee payroll schedule override'
        cfg = PayrollScheduleService.save_schedule(
            scope=ScheduleConfigScope.EMPLOYEE,
            business=emp.business,
            centre=emp.branch,
            employee=emp,
            data=request.data,
            user=request.user,
            reason=reason
        )

        res = PayrollScheduleService.resolve_schedule(employee=emp)
        res['can_manage'] = True
        return Response(res, status=status.HTTP_200_OK)


class EmployeePayrollScheduleResetView(views.APIView):
    """
    Reverts an employee-specific override to inherit from Centre or Enterprise defaults.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        ctx = get_user_context(request)
        emp = Employee.objects.filter(id=pk).select_related('business', 'branch').first()
        if not emp:
            raise NotFound('Employee not found.')

        if not ctx['is_superadmin'] and emp.business_id != ctx['business'].id:
            raise PermissionDenied('Access forbidden.')

        can_manage = (
            ctx['is_superadmin'] or
            ctx['role'] == BusinessRole.BUSINESS_ADMIN or
            (ctx['role'] == BusinessRole.MANAGER and PermissionService.has_permission(request.user, 'salary.edit', business=emp.business, centre=emp.branch))
        )
        if not can_manage:
            raise PermissionDenied('You do not have permission to reset employee payroll schedule overrides.')

        reason = request.data.get('change_reason') or f"Reverted employee {emp.full_name} to centre/enterprise defaults"
        PayrollScheduleService.reset_override(
            scope=ScheduleConfigScope.EMPLOYEE,
            business=emp.business,
            centre=emp.branch,
            employee=emp,
            user=request.user,
            reason=reason
        )

        res = PayrollScheduleService.resolve_schedule(employee=emp)
        res['can_manage'] = True
        return Response(res, status=status.HTTP_200_OK)


class CentrePayrollScheduleView(views.APIView):
    """
    GET: Resolves effective payroll schedule for a Centre (Centre Override vs Enterprise Default).
    POST: Creates or updates Centre-specific payroll schedule override.
    """
    permission_classes = [IsAuthenticated]

    def get_centre(self, request, pk):
        ctx = get_user_context(request)
        centre = Branch.objects.filter(id=pk).select_related('business').first()
        if not centre:
            raise NotFound('Centre not found.')

        if not ctx['is_superadmin'] and centre.business_id != ctx['business'].id:
            raise PermissionDenied('Access forbidden.')

        return centre

    def check_can_manage(self, request, centre):
        ctx = get_user_context(request)
        if ctx['is_superadmin']:
            return True
        if ctx['role'] == BusinessRole.BUSINESS_ADMIN and centre.business_id == ctx['business'].id:
            return True
        if ctx['role'] == BusinessRole.MANAGER:
            return PermissionService.has_permission(request.user, 'organization.manage_branches', business=centre.business, centre=centre)
        return False

    def get(self, request, pk):
        centre = self.get_centre(request, pk)
        data = PayrollScheduleService.resolve_schedule(centre=centre)
        data['can_manage'] = self.check_can_manage(request, centre)
        return Response(data)

    def post(self, request, pk):
        centre = self.get_centre(request, pk)
        if not self.check_can_manage(request, centre):
            raise PermissionDenied('You do not have permission to configure centre payroll schedule overrides.')

        reason = request.data.get('change_reason') or f"Updated payroll schedule override for centre {centre.name}"
        PayrollScheduleService.save_schedule(
            scope=ScheduleConfigScope.CENTRE,
            business=centre.business,
            centre=centre,
            employee=None,
            data=request.data,
            user=request.user,
            reason=reason
        )

        res = PayrollScheduleService.resolve_schedule(centre=centre)
        res['can_manage'] = True
        return Response(res, status=status.HTTP_200_OK)


class CentrePayrollScheduleResetView(views.APIView):
    """
    Reverts a Centre override to inherit Enterprise defaults.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        ctx = get_user_context(request)
        centre = Branch.objects.filter(id=pk).select_related('business').first()
        if not centre:
            raise NotFound('Centre not found.')

        if not ctx['is_superadmin'] and centre.business_id != ctx['business'].id:
            raise PermissionDenied('Access forbidden.')

        can_manage = (
            ctx['is_superadmin'] or
            ctx['role'] == BusinessRole.BUSINESS_ADMIN or
            (ctx['role'] == BusinessRole.MANAGER and PermissionService.has_permission(request.user, 'organization.manage_branches', business=centre.business, centre=centre))
        )
        if not can_manage:
            raise PermissionDenied('You do not have permission to reset centre payroll schedule overrides.')

        reason = request.data.get('change_reason') or f"Reverted centre {centre.name} to enterprise defaults"
        PayrollScheduleService.reset_override(
            scope=ScheduleConfigScope.CENTRE,
            business=centre.business,
            centre=centre,
            employee=None,
            user=request.user,
            reason=reason
        )

        res = PayrollScheduleService.resolve_schedule(centre=centre)
        res['can_manage'] = True
        return Response(res, status=status.HTTP_200_OK)


class EnterprisePayrollScheduleView(views.APIView):
    """
    GET: Resolves Enterprise default payroll schedule.
    POST: Creates or updates Enterprise default payroll schedule.
    """
    permission_classes = [IsAuthenticated]

    def get_business(self, request):
        ctx = get_user_context(request)
        biz = ctx.get('business')
        if not biz and ctx.get('is_superadmin'):
            biz_id = request.query_params.get('business_id') or request.data.get('business_id')
            if biz_id:
                biz = Business.objects.filter(id=biz_id).first()
        if not biz:
            raise PermissionDenied('No active enterprise context found.')
        return biz

    def get(self, request):
        biz = self.get_business(request)
        data = PayrollScheduleService.resolve_schedule(business=biz)
        ctx = get_user_context(request)
        data['can_manage'] = ctx['is_superadmin'] or ctx['role'] == BusinessRole.BUSINESS_ADMIN
        return Response(data)

    def post(self, request):
        ctx = get_user_context(request)
        if not (ctx['is_superadmin'] or ctx['role'] == BusinessRole.BUSINESS_ADMIN):
            raise PermissionDenied('Only Enterprise Administrators can edit enterprise payroll schedule defaults.')

        biz = self.get_business(request)
        reason = request.data.get('change_reason') or 'Updated enterprise payroll schedule defaults'
        PayrollScheduleService.save_schedule(
            scope=ScheduleConfigScope.ENTERPRISE,
            business=biz,
            centre=None,
            employee=None,
            data=request.data,
            user=request.user,
            reason=reason
        )

        res = PayrollScheduleService.resolve_schedule(business=biz)
        res['can_manage'] = True
        return Response(res, status=status.HTTP_200_OK)


class PayrollSchedulePeriodsPreviewView(views.APIView):
    """
    Returns contiguous calculated recent periods and expected payment dates
    for a given employee, centre, or enterprise scope.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx.get('business')

        emp_id = request.query_params.get('employee_id')
        centre_id = request.query_params.get('centre_id')

        employee = None
        centre = None

        if emp_id:
            employee = Employee.objects.filter(id=emp_id).select_related('business', 'branch').first()
            if employee:
                biz = employee.business
        elif centre_id and centre_id not in ['all', 'ALL', 'null', '']:
            centre = Branch.objects.filter(id=centre_id).select_related('business').first()
            if centre:
                biz = centre.business

        if not biz:
            raise NotFound('Enterprise context required.')

        schedule_res = PayrollScheduleService.resolve_schedule(employee=employee, centre=centre, business=biz)
        eff_cfg = schedule_res['effective_config']

        ref_date = timezone.now().date()
        ref_param = request.query_params.get('reference_date')
        if ref_param:
            from datetime import datetime
            try:
                ref_date = datetime.strptime(ref_param, '%Y-%m-%d').date()
            except ValueError:
                pass

        recent_periods = PayrollScheduleService.generate_recent_periods(eff_cfg, count=6, as_of=ref_date)
        return Response({
            'scope': schedule_res['source'],
            'source_display': schedule_res['source_display'],
            'effective_config': eff_cfg,
            'current_period': schedule_res['current_period'],
            'expected_payment_date': schedule_res['expected_payment_date'],
            'periods': recent_periods,
        })
