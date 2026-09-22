from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, NotFound

from apps.core.permissions import get_user_context
from apps.organization.models import BusinessRole
from apps.payroll.models import Payroll
from apps.payroll.serializers import PayrollSerializer


class PayrollListView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        if ctx.get('role') == BusinessRole.BROKER:
            raise PermissionDenied('Brokers do not have access to payroll records.')
        biz = ctx['business']

        if ctx['is_superadmin']:
            qs = Payroll.objects.all()
            biz_id = request.query_params.get('business_id')
            if biz_id:
                qs = qs.filter(business_id=biz_id)
        elif ctx['role'] == BusinessRole.BUSINESS_ADMIN:
            qs = Payroll.objects.filter(business=biz)
        elif ctx['role'] == BusinessRole.MANAGER:
            # Manager sees payroll records for their assigned staff
            if ctx.get('employee'):
                qs = Payroll.objects.filter(
                    business=biz,
                    employee__manager=ctx['employee']
                )
            else:
                qs = Payroll.objects.none()
        else:
            # Staff sees only own payroll records
            if ctx.get('employee'):
                qs = Payroll.objects.filter(employee=ctx['employee'])
            else:
                qs = Payroll.objects.none()

        emp_param = request.query_params.get('employee_id')
        if emp_param:
            qs = qs.filter(employee_id=emp_param)

        period_start = request.query_params.get('period_start')
        if period_start:
            qs = qs.filter(period_start__gte=period_start)

        status_param = request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param.upper())

        qs = qs.select_related('employee', 'employee__department').prefetch_related('payslip').order_by('-period_start')
        return Response(PayrollSerializer(qs[:100], many=True).data)


class PayrollDetailView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        ctx = get_user_context(request)
        payroll = Payroll.objects.filter(id=pk).select_related('employee', 'employee__department').prefetch_related('payslip').first()
        if not payroll:
            raise NotFound('Payroll record not found.')

        # Permission check
        if ctx['is_superadmin']:
            pass
        elif ctx['role'] == BusinessRole.BUSINESS_ADMIN:
            if payroll.business_id != ctx['business'].id:
                raise PermissionDenied('Access forbidden.')
        elif ctx['role'] == BusinessRole.MANAGER:
            if not ctx.get('employee') or payroll.employee.manager_id != ctx['employee'].id:
                raise PermissionDenied('Managers can only view payroll for their assigned staff.')
        else:
            if not ctx.get('employee') or payroll.employee_id != ctx['employee'].id:
                raise PermissionDenied('Access forbidden.')

        return Response(PayrollSerializer(payroll).data)
