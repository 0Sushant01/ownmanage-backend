from datetime import date
from django.utils import timezone
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
from django.db.models import Count, Q, Prefetch
from apps.attendance.models import AttendanceDay, AttendanceStatus
from apps.leaves.models import LeaveRequest, LeaveRequestStatus


def get_annotated_businesses_queryset():
    from apps.subscriptions.models import SubscriptionPayment
    return Business.objects.select_related(
        'subscription__plan',
        'referral__broker'
    ).prefetch_related(
        Prefetch(
            'subscription__payments',
            queryset=SubscriptionPayment.objects.order_by('-billing_date'),
            to_attr='prefetched_payments'
        )
    ).annotate(
        annotated_total_centres=Count('branches', filter=Q(branches__is_active=True), distinct=True),
        annotated_active_employees_count=Count('employees', filter=Q(employees__employment_status='ACTIVE'), distinct=True),
        annotated_managers_count=Count('memberships', filter=Q(memberships__role=BusinessRole.MANAGER, memberships__is_active=True), distinct=True),
    )


class BusinessListCreateView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        if ctx['is_superadmin']:
            qs = get_annotated_businesses_queryset().order_by('-created_at')
            return Response(BusinessSerializer(qs, many=True).data)
        elif ctx['business']:
            biz = get_annotated_businesses_queryset().filter(id=ctx['business'].id).first()
            return Response(BusinessSerializer([biz] if biz else [], many=True).data)
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
            biz = get_annotated_businesses_queryset().filter(id=pk).first()
            if not biz:
                raise NotFound('Business not found.')
            return biz
        if ctx['business'] and str(ctx['business'].id) == str(pk):
            biz = get_annotated_businesses_queryset().filter(id=pk).first()
            return biz or ctx['business']
        raise PermissionDenied('You do not have access to this business.')

    def get(self, request, pk):
        biz = self.get_object(request, pk)
        return Response(BusinessSerializer(biz).data)

    def patch(self, request, pk):
        biz = self.get_object(request, pk)
        ctx = get_user_context(request)
        if not ctx['is_superadmin'] and ctx['role'] != BusinessRole.BUSINESS_ADMIN:
            raise PermissionDenied('Only Business Admin or SuperAdmin can update business details.')
        old_active = biz.is_active
        serializer = BusinessSerializer(biz, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated_biz = serializer.save()

        if 'is_active' in request.data and updated_biz.is_active != old_active:
            from apps.core.audit import record_audit_log
            action = 'BUSINESS_REACTIVATED' if updated_biz.is_active else 'BUSINESS_DEACTIVATED'
            record_audit_log(
                action=action,
                entity_type='Business',
                entity_id=str(updated_biz.id),
                actor=request.user,
                business=updated_biz,
                old_data={'is_active': old_active},
                new_data={'is_active': updated_biz.is_active},
                request=request
            )

        return Response(BusinessSerializer(updated_biz).data)


class BusinessCentresCapacityView(views.APIView):
    """
    Returns all centres belonging to a business with their capacity allocation
    and active employee count for SuperAdmin/Business Admin capacity management.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        ctx = get_user_context(request)
        if not ctx['is_superadmin'] and (not ctx['business'] or str(ctx['business'].id) != str(pk)):
            raise PermissionDenied('Unauthorized access.')

        biz = Business.objects.filter(id=pk).first()
        if not biz:
            raise NotFound('Business not found.')

        branches = biz.branches.filter(is_active=True).order_by('name')
        return Response(BranchSerializer(branches, many=True).data)



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

        import zoneinfo
        tz_name = biz.timezone or 'Asia/Kolkata'
        try:
            biz_tz = zoneinfo.ZoneInfo(tz_name)
        except Exception:
            biz_tz = zoneinfo.ZoneInfo('Asia/Kolkata')

        today = timezone.now().astimezone(biz_tz).date()

        centre_id = request.query_params.get('centre_id') or request.query_params.get('branch_id')
        centre_obj = Branch.resolve_branch(centre_id, business=biz)
        if centre_obj:
            centre_id = str(centre_obj.id)

        # Center Manager role lock
        if ctx['role'] == BusinessRole.MANAGER and ctx.get('employee') and ctx['employee'].branch_id:
            if not centre_obj:
                centre_obj = ctx['employee'].branch
                centre_id = str(centre_obj.id) if centre_obj else None

        # Base querysets
        centres_qs = Branch.objects.filter(business=biz)
        emp_qs = Employee.objects.filter(business=biz)
        att_qs = AttendanceDay.objects.filter(business=biz, attendance_date=today)
        leave_qs = LeaveRequest.objects.filter(
            business=biz,
            status=LeaveRequestStatus.APPROVED,
            start_date__lte=today,
            end_date__gte=today
        )

        if centre_obj:
            centres_qs = centres_qs.filter(id=centre_obj.id)
            emp_qs = emp_qs.filter(branch=centre_obj)
            att_qs = att_qs.filter(Q(centre=centre_obj) | Q(employee__branch=centre_obj))
            leave_qs = leave_qs.filter(employee__branch=centre_obj)

        total_centres = centres_qs.count()
        active_centres = centres_qs.filter(is_active=True).count()

        total_employees = emp_qs.count()
        active_employees = emp_qs.filter(employment_status=EmploymentStatus.ACTIVE).count()

        present_today = att_qs.filter(status__in=[AttendanceStatus.PRESENT, AttendanceStatus.OVERTIME]).count()
        late_today = att_qs.filter(status=AttendanceStatus.LATE).count()
        half_day_today = att_qs.filter(status=AttendanceStatus.HALF_DAY).count()
        overtime_today = att_qs.filter(Q(status=AttendanceStatus.OVERTIME) | Q(overtime_seconds__gt=0)).count()
        on_leave = leave_qs.count()

        total_reported = present_today + late_today + half_day_today + on_leave
        absent_today = max(0, active_employees - total_reported)

        return Response({
            'business_id': str(biz.id),
            'business_name': biz.name,
            'selected_centre_id': str(centre_obj.id) if centre_obj else None,
            'selected_centre_name': centre_obj.name if centre_obj else 'All Centres',
            'date': str(today),
            'total_centres': total_centres,
            'active_centres': active_centres,
            'total_employees': total_employees,
            'active_employees': active_employees,
            'present_today': present_today,
            'late_today': late_today,
            'half_day_today': half_day_today,
            'overtime_today': overtime_today,
            'on_leave': on_leave,
            'absent_today': absent_today,
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

        centre_id = request.query_params.get('centre_id') or request.query_params.get('branch_id')
        if centre_id and centre_id not in ['all', 'ALL', 'null', '']:
            branch_obj = Branch.resolve_branch(centre_id, business=biz)
            if branch_obj:
                qs = qs.filter(branch_id=branch_obj.id)
            else:
                qs = qs.none()

        qs = qs.select_related('user', 'branch', 'department')
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
            # Manager sees staff in their assigned Center or their direct reports
            if ctx.get('employee'):
                mgr_emp = ctx['employee']
                from django.db.models import Q
                mgr_filter = Q(manager=mgr_emp)
                if mgr_emp.branch_id:
                    mgr_filter |= Q(branch_id=mgr_emp.branch_id)
                qs = Employee.objects.filter(business=biz).filter(mgr_filter)
            else:
                qs = Employee.objects.none()
        else:
            # Staff sees only self
            if ctx.get('employee'):
                qs = Employee.objects.filter(id=ctx['employee'].id)
            else:
                qs = Employee.objects.none()

        centre_filter = request.query_params.get('centre_id') or request.query_params.get('branch_id')
        if centre_filter and centre_filter not in ['all', 'ALL', 'null', '']:
            branch_obj = Branch.resolve_branch(centre_filter, business=biz)
            if branch_obj:
                qs = qs.filter(branch_id=branch_obj.id)
            else:
                qs = qs.none()

        dept_filter = request.query_params.get('department_id')
        if dept_filter and dept_filter not in ['all', 'ALL', 'null', '']:
            qs = qs.filter(department_id=dept_filter)

        mgr_filter_param = request.query_params.get('manager_id')
        if mgr_filter_param and mgr_filter_param not in ['all', 'ALL', 'null', '']:
            qs = qs.filter(manager_id=mgr_filter_param)

        status_filter = request.query_params.get('status')
        if status_filter and status_filter not in ['all', 'ALL', 'null', '']:
            qs = qs.filter(employment_status=status_filter.upper())

        search_query = request.query_params.get('search')
        if search_query:
            qs = qs.filter(
                Q(first_name__icontains=search_query) |
                Q(last_name__icontains=search_query) |
                Q(employee_id__icontains=search_query) |
                Q(email__icontains=search_query)
            )

        from apps.payroll.models import SalaryRevision
        qs = qs.select_related('department', 'branch', 'manager').prefetch_related(
            Prefetch('salary_revisions', queryset=SalaryRevision.objects.order_by('-effective_from'), to_attr='_prefetched_salary_revisions')
        )
        return Response(EmployeeListSerializer(qs.order_by('first_name', 'last_name'), many=True).data)

    def post(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if ctx['is_superadmin']:
            biz_id = request.data.get('business_id') or request.query_params.get('business_id')
            if biz_id:
                biz = Business.objects.filter(id=biz_id).first()

        if not biz:
            raise PermissionDenied('A valid business is required to create an employee.')

        from apps.organization.services.permission_service import PermissionService
        if not PermissionService.has_permission(request.user, 'employees.create', business=biz):
            raise PermissionDenied('You do not have permission to create employee records.')

        serializer = EmployeeCreateSerializer(data=request.data, context={'business': biz})
        serializer.is_valid(raise_exception=True)
        emp = serializer.save()

        # Log employee creation timeline event
        from apps.organization.models import EmployeeActivityLog
        EmployeeActivityLog.objects.create(
            business=biz,
            employee=emp,
            activity_type='EMPLOYEE_CREATED',
            description=f"Employee profile created: {emp.full_name} ({emp.employee_id})",
            new_value={'employee_id': emp.employee_id, 'branch': emp.branch.name if emp.branch else None},
            performed_by=request.user
        )

        return Response(EmployeeDetailSerializer(emp).data, status=status.HTTP_201_CREATED)


class EmployeeDetailView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get_object(self, request, pk):
        emp = Employee.objects.filter(id=pk).select_related('business').first()
        if not emp:
            raise NotFound('Employee not found.')

        from apps.organization.services.permission_service import PermissionService
        if not PermissionService.has_permission(request.user, 'employees.view', business=emp.business, target_employee=emp):
            raise PermissionDenied('Access denied to employee record.')

        return emp

    def get(self, request, pk):
        emp = self.get_object(request, pk)
        return Response(EmployeeDetailSerializer(emp).data)

    def patch(self, request, pk):
        emp = self.get_object(request, pk)
        from apps.organization.services.permission_service import PermissionService
        if not PermissionService.has_permission(request.user, 'employees.edit', business=emp.business, target_employee=emp):
            raise PermissionDenied('You do not have permission to edit employee records.')

        old_branch_id = emp.branch_id
        old_branch_name = emp.branch.name if emp.branch else 'None'
        old_mgr_name = emp.manager.full_name if emp.manager else 'None'
        old_status = emp.employment_status

        serializer = EmployeeDetailSerializer(emp, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated_emp = serializer.save()

        from apps.organization.models import EmployeeActivityLog
        if ('branch' in request.data or 'branch_id' in request.data) and updated_emp.branch_id != old_branch_id:
            new_branch_name = updated_emp.branch.name if updated_emp.branch else 'None'
            EmployeeActivityLog.objects.create(
                business=emp.business,
                employee=emp,
                activity_type='CENTRE_CHANGED',
                description=f"Centre changed: {old_branch_name} → {new_branch_name}",
                old_value={'branch': old_branch_name},
                new_value={'branch': new_branch_name},
                performed_by=request.user
            )
            from apps.attendance.services.employee_working_hours_service import EmployeeWorkingHoursService
            EmployeeWorkingHoursService.sync_employee_centre_change(updated_emp, updated_emp.branch)

        if 'manager' in request.data and updated_emp.manager != emp.manager:
            new_mgr_name = updated_emp.manager.full_name if updated_emp.manager else 'None'
            EmployeeActivityLog.objects.create(
                business=emp.business,
                employee=emp,
                activity_type='MANAGER_CHANGED',
                description=f"Manager changed: {old_mgr_name} → {new_mgr_name}",
                old_value={'manager': old_mgr_name},
                new_value={'manager': new_mgr_name},
                performed_by=request.user
            )

        if 'employment_status' in request.data and updated_emp.employment_status != old_status:
            EmployeeActivityLog.objects.create(
                business=emp.business,
                employee=emp,
                activity_type='STATUS_CHANGED',
                description=f"Status changed: {old_status} → {updated_emp.employment_status}",
                old_value={'employment_status': old_status},
                new_value={'employment_status': updated_emp.employment_status},
                performed_by=request.user
            )

        profile_fields = ['first_name', 'last_name', 'email', 'phone', 'date_of_birth', 'address', 'emergency_contact', 'designation', 'department']
        changed_fields = [f for f in profile_fields if f in request.data]
        if changed_fields:
            EmployeeActivityLog.objects.create(
                business=emp.business,
                employee=emp,
                activity_type='PROFILE_UPDATED',
                description=f"Profile details updated ({', '.join(changed_fields)})",
                new_value={f: str(getattr(updated_emp, f, '')) for f in changed_fields},
                performed_by=request.user
            )

        return Response(EmployeeDetailSerializer(updated_emp).data)


class EmployeeDeactivateView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        emp = Employee.objects.filter(id=pk).select_related('business').first()
        if not emp:
            raise NotFound('Employee not found.')

        from apps.organization.services.permission_service import PermissionService
        if not PermissionService.has_permission(request.user, 'employees.change_status', business=emp.business, target_employee=emp):
            raise PermissionDenied('You do not have permission to change employee status.')

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


class EmployeeActivityLogView(views.APIView):
    """
    Returns timeline activity log for an employee (who, what, when, old_value, new_value).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        emp = Employee.objects.filter(id=pk).select_related('business').first()
        if not emp:
            raise NotFound('Employee not found.')

        ctx = get_user_context(request)
        if not ctx['is_superadmin'] and emp.business_id != ctx['business'].id:
            raise PermissionDenied('Cross-tenant employee access forbidden.')

        from apps.organization.models import EmployeeActivityLog
        from apps.organization.serializers import EmployeeActivityLogSerializer

        logs = EmployeeActivityLog.objects.filter(employee=emp).select_related('performed_by').order_by('-created_at')
        return Response(EmployeeActivityLogSerializer(logs[:100], many=True).data)

