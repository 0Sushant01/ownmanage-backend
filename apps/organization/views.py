from datetime import date
from rest_framework import status, views, viewsets
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, NotFound

from apps.core.permissions import get_user_context, IsSuperAdmin, IsBusinessAdmin, IsManagerOrAdmin
from apps.organization.models import (
    Business, BusinessMembership, BusinessRole,
    Branch, Department, Employee, EmploymentStatus
)
from apps.organization.serializers import (
    BusinessSerializer, BranchSerializer, DepartmentSerializer,
    EmployeeListSerializer, EmployeeDetailSerializer, EmployeeCreateSerializer,
    ManagerCreateSerializer, BusinessCreateAdminSerializer
)
from apps.attendance.models import AttendanceDay, AttendanceStatus
from apps.leaves.models import LeaveRequest, LeaveRequestStatus


class BusinessListCreateView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        if ctx['is_superadmin']:
            qs = Business.objects.all().order_by('-created_at')
            return Response(BusinessSerializer(qs, many=True).data)
        elif ctx['business']:
            return Response(BusinessSerializer([ctx['business']], many=True).data)
        raise PermissionDenied('No business association found.')

    def post(self, request):
        ctx = get_user_context(request)
        if not ctx['is_superadmin']:
            raise PermissionDenied('Only SuperAdmin can create new businesses.')
        serializer = BusinessSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        biz = serializer.save()
        return Response(BusinessSerializer(biz).data, status=status.HTTP_201_CREATED)


class BusinessDetailView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, request, pk):
        ctx = get_user_context(request)
        if ctx['is_superadmin']:
            biz = Business.objects.filter(id=pk).first()
            if not biz:
                raise NotFound('Business not found.')
            return biz
        if ctx['business'] and str(ctx['business'].id) == str(pk):
            return ctx['business']
        raise PermissionDenied('You do not have access to this business.')

    def get(self, request, pk):
        biz = self.get_object(request, pk)
        return Response(BusinessSerializer(biz).data)

    def patch(self, request, pk):
        biz = self.get_object(request, pk)
        ctx = get_user_context(request)
        if not ctx['is_superadmin'] and ctx['role'] != BusinessRole.BUSINESS_ADMIN:
            raise PermissionDenied('Only Business Admin or SuperAdmin can update business details.')
        serializer = BusinessSerializer(biz, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(BusinessSerializer(biz).data)


class BusinessStatsView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk=None):
        ctx = get_user_context(request)
        biz = None
        if ctx['is_superadmin']:
            if pk:
                biz = Business.objects.filter(id=pk).first()
            if not biz:
                # Platform-wide metrics for SuperAdmin dashboard
                total_biz = Business.objects.count()
                active_biz = Business.objects.filter(is_active=True).count()
                total_mgr = BusinessMembership.objects.filter(role=BusinessRole.MANAGER, is_active=True).count()
                total_emp = Employee.objects.filter(employment_status=EmploymentStatus.ACTIVE).count()
                return Response({
                    'total_businesses': total_biz,
                    'active_businesses': active_biz,
                    'total_managers': total_mgr,
                    'total_employees': total_emp,
                })
        else:
            biz = ctx['business']

        if not biz:
            raise PermissionDenied('No business context available.')

        today = date.today()
        total_emp = Employee.objects.filter(business=biz, employment_status=EmploymentStatus.ACTIVE).count()
        present_today = AttendanceDay.objects.filter(business=biz, attendance_date=today, status=AttendanceStatus.PRESENT).count()
        on_leave = LeaveRequest.objects.filter(
            business=biz,
            status=LeaveRequestStatus.APPROVED,
            start_date__lte=today,
            end_date__gte=today
        ).count()
        absent_today = max(0, total_emp - (present_today + on_leave))

        return Response({
            'business_id': str(biz.id),
            'business_name': biz.name,
            'total_employees': total_emp,
            'present_today': present_today,
            'absent_today': absent_today,
            'on_leave': on_leave,
        })


class BusinessCreateAdminView(views.APIView):
    permission_classes = [IsSuperAdmin]

    def post(self, request, pk):
        biz = Business.objects.filter(id=pk).first()
        if not biz:
            raise NotFound('Business not found.')
        serializer = BusinessCreateAdminSerializer(data=request.data, context={'business': biz})
        serializer.is_valid(raise_exception=True)
        membership = serializer.save()
        return Response({
            'detail': f"Business Admin created for {biz.name}.",
            'admin_email': membership.user.email,
        }, status=status.HTTP_201_CREATED)


class ManagerListView(views.APIView):
    permission_classes = [IsBusinessAdmin]

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if ctx['is_superadmin']:
            biz_id = request.query_params.get('business_id')
            if biz_id:
                biz = Business.objects.filter(id=biz_id).first()

        qs = Employee.objects.filter(
            business=biz,
            user__business_memberships__role=BusinessRole.MANAGER,
            user__business_memberships__is_active=True
        ).distinct() if biz else Employee.objects.none()

        return Response(EmployeeListSerializer(qs, many=True).data)

    def post(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if ctx['is_superadmin']:
            biz_id = request.data.get('business_id') or request.query_params.get('business_id')
            if biz_id:
                biz = Business.objects.filter(id=biz_id).first()

        if not biz:
            raise PermissionDenied('A valid business is required to create a manager.')

        serializer = ManagerCreateSerializer(data=request.data, context={'business': biz})
        serializer.is_valid(raise_exception=True)
        mgr = serializer.save()
        return Response(EmployeeDetailSerializer(mgr).data, status=status.HTTP_201_CREATED)


class ManagerDetailView(views.APIView):
    permission_classes = [IsBusinessAdmin]

    def get(self, request, pk):
        ctx = get_user_context(request)
        emp = Employee.objects.filter(id=pk).first()
        if not emp:
            raise NotFound('Manager not found.')
        if not ctx['is_superadmin'] and emp.business_id != ctx['business'].id:
            raise PermissionDenied('Unauthorized access.')
        return Response(EmployeeDetailSerializer(emp).data)

    def patch(self, request, pk):
        ctx = get_user_context(request)
        emp = Employee.objects.filter(id=pk).first()
        if not emp:
            raise NotFound('Manager not found.')
        if not ctx['is_superadmin'] and emp.business_id != ctx['business'].id:
            raise PermissionDenied('Unauthorized access.')

        serializer = EmployeeDetailSerializer(emp, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(EmployeeDetailSerializer(emp).data)


class EmployeeListView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        if ctx.get('role') == BusinessRole.BROKER:
            raise PermissionDenied('Brokers do not have access to enterprise employee records.')
        biz = ctx['business']

        if ctx['is_superadmin']:
            biz_id = request.query_params.get('business_id')
            qs = Employee.objects.all()
            if biz_id:
                qs = qs.filter(business_id=biz_id)
        elif ctx['role'] == BusinessRole.BUSINESS_ADMIN:
            qs = Employee.objects.filter(business=biz)
        elif ctx['role'] == BusinessRole.MANAGER:
            # Manager sees only their assigned direct reports
            if ctx.get('employee'):
                qs = Employee.objects.filter(business=biz, manager=ctx['employee'])
            else:
                qs = Employee.objects.none()
        else:
            # Staff sees only self
            if ctx.get('employee'):
                qs = Employee.objects.filter(id=ctx['employee'].id)
            else:
                qs = Employee.objects.none()

        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(employment_status=status_filter.upper())

        return Response(EmployeeListSerializer(qs.order_by('first_name'), many=True).data)

    def post(self, request):
        ctx = get_user_context(request)
        if not (ctx['is_superadmin'] or ctx['role'] == BusinessRole.BUSINESS_ADMIN):
            raise PermissionDenied('Only Business Admin or SuperAdmin can create employees.')

        biz = ctx['business']
        if ctx['is_superadmin']:
            biz_id = request.data.get('business_id') or request.query_params.get('business_id')
            if biz_id:
                biz = Business.objects.filter(id=biz_id).first()

        if not biz:
            raise PermissionDenied('A valid business is required to create an employee.')

        serializer = EmployeeCreateSerializer(data=request.data, context={'business': biz})
        serializer.is_valid(raise_exception=True)
        emp = serializer.save()
        return Response(EmployeeDetailSerializer(emp).data, status=status.HTTP_201_CREATED)


class EmployeeDetailView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, request, pk):
        ctx = get_user_context(request)
        emp = Employee.objects.filter(id=pk).first()
        if not emp:
            raise NotFound('Employee not found.')

        if ctx['is_superadmin']:
            return emp
        if emp.business_id != ctx['business'].id:
            raise PermissionDenied('Cross-tenant access forbidden.')
        if ctx['role'] == BusinessRole.BUSINESS_ADMIN:
            return emp
        if ctx['role'] == BusinessRole.MANAGER:
            if ctx.get('employee') and (emp.manager_id == ctx['employee'].id or emp.id == ctx['employee'].id):
                return emp
            raise PermissionDenied('Manager cannot access staff not assigned to them.')
        if ctx.get('employee') and emp.id == ctx['employee'].id:
            return emp
        raise PermissionDenied('Access denied.')

    def get(self, request, pk):
        emp = self.get_object(request, pk)
        return Response(EmployeeDetailSerializer(emp).data)

    def patch(self, request, pk):
        emp = self.get_object(request, pk)
        ctx = get_user_context(request)
        if not (ctx['is_superadmin'] or ctx['role'] == BusinessRole.BUSINESS_ADMIN):
            raise PermissionDenied('Only Business Admin can edit employee records.')

        serializer = EmployeeDetailSerializer(emp, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(EmployeeDetailSerializer(emp).data)


class EmployeeDeactivateView(views.APIView):
    permission_classes = [IsBusinessAdmin]

    def post(self, request, pk):
        ctx = get_user_context(request)
        emp = Employee.objects.filter(id=pk).first()
        if not emp:
            raise NotFound('Employee not found.')
        if not ctx['is_superadmin'] and emp.business_id != ctx['business'].id:
            raise PermissionDenied('Cross-tenant operation forbidden.')

        emp.employment_status = EmploymentStatus.TERMINATED
        emp.date_of_exit = date.today()
        emp.save(update_fields=['employment_status', 'date_of_exit', 'updated_at'])

        if emp.user:
            emp.user.is_active = False
            emp.user.save(update_fields=['is_active'])

        return Response({'detail': f'Employee {emp.full_name} has been deactivated.'})


class EmployeeMetadataView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if ctx['is_superadmin']:
            biz_id = request.query_params.get('business_id')
            if biz_id:
                biz = Business.objects.filter(id=biz_id).first()

        if not biz:
            return Response({'departments': [], 'branches': [], 'managers': []})

        departments = Department.objects.filter(business=biz, is_active=True).values('id', 'name', 'code')
        branches = Branch.objects.filter(business=biz, is_active=True).values('id', 'name', 'code')
        managers = Employee.objects.filter(
            business=biz,
            user__business_memberships__role=BusinessRole.MANAGER,
            employment_status=EmploymentStatus.ACTIVE
        ).values('id', 'first_name', 'last_name', 'employee_id')

        return Response({
            'departments': list(departments),
            'branches': list(branches),
            'managers': [
                {'id': str(m['id']), 'name': f"{m['first_name']} {m['last_name']}".strip(), 'employee_id': m['employee_id']}
                for m in managers
            ],
        })


class CentreListCreateView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if ctx['is_superadmin']:
            biz_id = request.query_params.get('business_id')
            if biz_id:
                biz = Business.objects.filter(id=biz_id).first()
            else:
                qs = Branch.objects.all().select_related('business').order_by('name')
                return Response(BranchSerializer(qs, many=True).data)

        if not biz:
            raise PermissionDenied('No business context available.')

        qs = Branch.objects.filter(business=biz).order_by('name')
        return Response(BranchSerializer(qs, many=True).data)

    def post(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if ctx['is_superadmin']:
            biz_id = request.data.get('business_id') or request.query_params.get('business_id')
            if biz_id:
                biz = Business.objects.filter(id=biz_id).first()

        if not biz:
            raise PermissionDenied('A valid business is required.')
        if not ctx['is_superadmin'] and ctx['role'] != BusinessRole.BUSINESS_ADMIN:
            raise PermissionDenied('Only Business Admin or SuperAdmin can create centres.')

        from apps.subscriptions.models import Subscription
        sub = Subscription.objects.filter(business=biz).select_related('plan').first()
        if sub:
            current_centres = Branch.objects.filter(business=biz, is_active=True).count()
            if current_centres >= sub.plan.max_centres:
                return Response({
                    'code': 'PLAN_CENTRE_LIMIT_EXCEEDED',
                    'detail': f"Maximum centre limit ({sub.plan.max_centres}) reached for current plan '{sub.plan.name}'. Upgrade your subscription to add more centres."
                }, status=status.HTTP_400_BAD_REQUEST)

        serializer = BranchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        centre = serializer.save(business=biz)
        return Response(BranchSerializer(centre).data, status=status.HTTP_201_CREATED)


class CentreDetailView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, request, pk):
        ctx = get_user_context(request)
        if ctx['is_superadmin']:
            centre = Branch.objects.filter(id=pk).first()
            if not centre:
                raise NotFound('Centre not found.')
            return centre
        if not ctx['business']:
            raise PermissionDenied('No business context.')
        centre = Branch.objects.filter(id=pk, business=ctx['business']).first()
        if not centre:
            raise NotFound('Centre not found in your enterprise.')
        return centre

    def get(self, request, pk):
        centre = self.get_object(request, pk)
        return Response(BranchSerializer(centre).data)

    def patch(self, request, pk):
        centre = self.get_object(request, pk)
        ctx = get_user_context(request)
        if not ctx['is_superadmin'] and ctx['role'] != BusinessRole.BUSINESS_ADMIN:
            raise PermissionDenied('Only Business Admin can update centre details.')
        serializer = BranchSerializer(centre, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(BranchSerializer(centre).data)


class DepartmentListCreateView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if ctx['is_superadmin']:
            biz_id = request.query_params.get('business_id')
            if biz_id:
                biz = Business.objects.filter(id=biz_id).first()
            else:
                qs = Department.objects.all().select_related('business').order_by('name')
                return Response(DepartmentSerializer(qs, many=True).data)

        if not biz:
            raise PermissionDenied('No business context available.')

        qs = Department.objects.filter(business=biz).order_by('name')
        return Response(DepartmentSerializer(qs, many=True).data)

    def post(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if ctx['is_superadmin']:
            biz_id = request.data.get('business_id')
            if biz_id:
                biz = Business.objects.filter(id=biz_id).first()

        if not biz:
            raise PermissionDenied('A valid business is required.')
        if not ctx['is_superadmin'] and ctx['role'] != BusinessRole.BUSINESS_ADMIN:
            raise PermissionDenied('Only Business Admin or SuperAdmin can create departments.')

        serializer = DepartmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        dept = serializer.save(business=biz)
        return Response(DepartmentSerializer(dept).data, status=status.HTTP_201_CREATED)

