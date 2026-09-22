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
            # Manager sees leave requests of their assigned staff
            if ctx.get('employee'):
                qs = LeaveRequest.objects.filter(
                    business=biz,
                    employee__manager=ctx['employee']
                )
            else:
                qs = LeaveRequest.objects.none()
        else:
            # Staff sees only their own requests
            if ctx.get('employee'):
                qs = LeaveRequest.objects.filter(employee=ctx['employee'])
            else:
                qs = LeaveRequest.objects.none()

        status_param = request.query_params.get('status')
        if status_param:
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
        leave_req = LeaveRequest.objects.filter(id=pk).select_related('employee').first()
        if not leave_req:
            raise NotFound('Leave request not found.')

        # Permission check: SuperAdmin, Business Admin of the business, or Manager of the employee
        can_approve = False
        if ctx['is_superadmin']:
            can_approve = True
        elif ctx['role'] == BusinessRole.BUSINESS_ADMIN and leave_req.business_id == ctx['business'].id:
            can_approve = True
        elif ctx['role'] == BusinessRole.MANAGER and ctx.get('employee') and leave_req.employee.manager_id == ctx['employee'].id:
            can_approve = True

        # A manager/user must never approve their own leave request
        if leave_req.employee.user_id == request.user.id:
            raise PermissionDenied('You cannot approve your own leave request.')

        if not can_approve:
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
        leave_req = LeaveRequest.objects.filter(id=pk).select_related('employee').first()
        if not leave_req:
            raise NotFound('Leave request not found.')

        can_reject = False
        if ctx['is_superadmin']:
            can_reject = True
        elif ctx['role'] == BusinessRole.BUSINESS_ADMIN and leave_req.business_id == ctx['business'].id:
            can_reject = True
        elif ctx['role'] == BusinessRole.MANAGER and ctx.get('employee') and leave_req.employee.manager_id == ctx['employee'].id:
            can_reject = True

        if not can_reject:
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
