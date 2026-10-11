from django.utils import timezone
from rest_framework import status, views
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, NotFound, ValidationError

from apps.core.permissions import get_user_context
from apps.organization.models import BusinessRole
from apps.leaves.models import LeaveType, LeaveRequest, LeaveRequestStatus
from apps.leaves.serializers import (
    LeaveTypeSerializer, LeaveRequestSerializer, LeaveRequestCreateSerializer
)


class LeaveTypeListView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if ctx['is_superadmin']:
            biz_id = request.query_params.get('business_id')
            if biz_id:
                biz = Business.objects.filter(id=biz_id).first()

        qs = LeaveType.objects.filter(business=biz, is_active=True) if biz else LeaveType.objects.none()
        return Response(LeaveTypeSerializer(qs, many=True).data)


class LeaveRequestListCreateView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']

        if ctx['is_superadmin']:
            qs = LeaveRequest.objects.all()
            biz_id = request.query_params.get('business_id')
            if biz_id:
                qs = qs.filter(business_id=biz_id)
        elif ctx['role'] == BusinessRole.BUSINESS_ADMIN:
            qs = LeaveRequest.objects.filter(business=biz)
        elif ctx['role'] == BusinessRole.MANAGER:
            # Manager sees leave requests of staff in their centre or direct reports
            if ctx.get('employee'):
                mgr_emp = ctx['employee']
                from django.db.models import Q
                mgr_filter = Q(employee__manager=mgr_emp)
                if mgr_emp.branch_id:
                    mgr_filter |= Q(employee__branch_id=mgr_emp.branch_id)
                qs = LeaveRequest.objects.filter(business=biz).filter(mgr_filter)
            else:
                qs = LeaveRequest.objects.none()
        else:
            # Staff sees only their own requests
            if ctx.get('employee'):
                qs = LeaveRequest.objects.filter(employee=ctx['employee'])
            else:
                qs = LeaveRequest.objects.none()

        centre_filter = request.query_params.get('centre_id') or request.query_params.get('branch_id')
        if centre_filter and centre_filter not in ['all', 'ALL', 'null', '']:
            from apps.organization.models import Branch
            branch_obj = Branch.resolve_branch(centre_filter, business=ctx.get('business'))
            if branch_obj:
                qs = qs.filter(employee__branch_id=branch_obj.id)
            else:
                qs = qs.none()

        status_param = request.query_params.get('status')
        if status_param and status_param not in ['all', 'ALL', 'null', '']:
            qs = qs.filter(status=status_param.upper())

        qs = qs.select_related('employee', 'leave_type', 'approved_by').order_by('-created_at')
        return Response(LeaveRequestSerializer(qs[:100], many=True).data)

    def post(self, request):
        ctx = get_user_context(request)
        emp = ctx.get('employee')
        if not emp:
            raise PermissionDenied('Only staff/employees with a linked profile can apply for leave.')

        serializer = LeaveRequestCreateSerializer(data=request.data, context={'employee': emp})
        serializer.is_valid(raise_exception=True)
        leave_req = serializer.save()
        return Response(LeaveRequestSerializer(leave_req).data, status=status.HTTP_201_CREATED)


class LeaveRequestApproveView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        ctx = get_user_context(request)
        leave_req = LeaveRequest.objects.filter(id=pk).select_related('employee', 'business').first()
        if not leave_req:
            raise NotFound('Leave request not found.')

        # A manager/user must never approve their own leave request
        if leave_req.employee.user_id == request.user.id:
            raise PermissionDenied('You cannot approve your own leave request.')

        from apps.organization.services.permission_service import PermissionService
        if not PermissionService.has_permission(request.user, 'leave.approve', business=leave_req.business, target_employee=leave_req.employee):
            raise PermissionDenied('You do not have permission to approve this leave request.')

        if leave_req.status != LeaveRequestStatus.PENDING:
            raise ValidationError({'detail': f'Cannot approve request that is already {leave_req.status}.'})

        leave_req.status = LeaveRequestStatus.APPROVED
        leave_req.approved_by = request.user
        leave_req.approved_at = timezone.now()
        leave_req.save(update_fields=['status', 'approved_by', 'approved_at', 'updated_at'])

        return Response({
            'detail': 'Leave request approved successfully.',
            'leave_request': LeaveRequestSerializer(leave_req).data
        })


class LeaveRequestRejectView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        ctx = get_user_context(request)
        leave_req = LeaveRequest.objects.filter(id=pk).select_related('employee', 'business').first()
        if not leave_req:
            raise NotFound('Leave request not found.')

        from apps.organization.services.permission_service import PermissionService
        if not PermissionService.has_permission(request.user, 'leave.reject', business=leave_req.business, target_employee=leave_req.employee):
            raise PermissionDenied('You do not have permission to reject this leave request.')

        if leave_req.status != LeaveRequestStatus.PENDING:
            raise ValidationError({'detail': f'Cannot reject request that is already {leave_req.status}.'})

        leave_req.status = LeaveRequestStatus.REJECTED
        leave_req.approved_by = request.user
        leave_req.rejected_at = timezone.now()
        leave_req.rejection_reason = request.data.get('reason', '')
        leave_req.save(update_fields=['status', 'approved_by', 'rejected_at', 'rejection_reason', 'updated_at'])

        return Response({
            'detail': 'Leave request rejected.',
            'leave_request': LeaveRequestSerializer(leave_req).data
        })


class LeaveRequestStatusUpdateView(views.APIView):
    """
    Inline status update endpoint for Leave Requests.
    Allows authorized administrators and managers to update status to PENDING, APPROVED, or REJECTED.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        ctx = get_user_context(request)
        leave_req = LeaveRequest.objects.filter(id=pk).select_related('employee', 'business').first()
        if not leave_req:
            raise NotFound('Leave request not found.')

        from apps.organization.services.permission_service import PermissionService
        can_manage = (
            ctx['is_superadmin'] or
            ctx['role'] == BusinessRole.BUSINESS_ADMIN or
            PermissionService.has_permission(request.user, 'leave.approve', business=leave_req.business, target_employee=leave_req.employee) or
            PermissionService.has_permission(request.user, 'leave.reject', business=leave_req.business, target_employee=leave_req.employee)
        )
        if not can_manage:
            raise PermissionDenied('You do not have permission to modify leave request status.')

        # Self-approval protection
        if leave_req.employee.user_id == request.user.id and not ctx['is_superadmin']:
            raise PermissionDenied('You cannot modify your own leave request status.')

        new_status = request.data.get('status')
        if not new_status or new_status not in [LeaveRequestStatus.PENDING, LeaveRequestStatus.APPROVED, LeaveRequestStatus.REJECTED]:
            raise ValidationError({'detail': 'Invalid status. Choose PENDING, APPROVED, or REJECTED.'})

        old_status = leave_req.status
        leave_req.status = new_status
        if new_status == LeaveRequestStatus.APPROVED:
            leave_req.approved_by = request.user
            leave_req.approved_at = timezone.now()
            leave_req.rejected_at = None
        elif new_status == LeaveRequestStatus.REJECTED:
            leave_req.approved_by = request.user
            leave_req.rejected_at = timezone.now()
            leave_req.rejection_reason = request.data.get('reason', '')
        elif new_status == LeaveRequestStatus.PENDING:
            leave_req.approved_by = None
            leave_req.approved_at = None
            leave_req.rejected_at = None
            leave_req.rejection_reason = ''

        leave_req.save()

        # Audit log
        from apps.core.services.audit_service import AuditService
        AuditService.log(
            user_or_request=request,
            action='UPDATE_LEAVE_STATUS',
            entity_type='LeaveRequest',
            entity_id=str(leave_req.id),
            old_data={'status': old_status},
            new_data={'status': new_status},
            business=leave_req.business,
            reason=f'Updated leave request status: {old_status} → {new_status}'
        )

        return Response({
            'detail': f'Leave request status updated to {new_status}.',
            'leave_request': LeaveRequestSerializer(leave_req).data
        })

