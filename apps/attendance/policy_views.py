from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, NotFound, ValidationError

from apps.core.permissions import get_user_context
from apps.organization.models import Branch, Business, BusinessRole
from apps.attendance.models import AttendancePolicy, AttendancePolicyOverride
from apps.attendance.serializers import AttendancePolicySerializer, AttendancePolicyOverrideSerializer
from apps.organization.services.policy_resolver import PolicyResolver
from apps.organization.services.permission_service import PermissionService
from apps.core.services.audit_service import AuditService


class EnterpriseAttendancePolicyView(views.APIView):
    """
    Enterprise Admin views and manages global attendance policy defaults.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if not biz:
            raise PermissionDenied('No active enterprise context.')

        policy_data = PolicyResolver.get_attendance_policy(business=biz, user=request.user)
        return Response(policy_data)

    def put(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if not (ctx['is_superadmin'] or ctx['role'] == BusinessRole.BUSINESS_ADMIN):
            raise PermissionDenied('Only Enterprise Admin can configure enterprise attendance policy.')

        policy, _ = AttendancePolicy.objects.get_or_create(business=biz)
        old_data = AttendancePolicySerializer(policy).data

        serializer = AttendancePolicySerializer(policy, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        policy = serializer.save()

        AuditService.log(
            user_or_request=request,
            action='UPDATE_ENTERPRISE_ATTENDANCE_POLICY',
            entity_type='AttendancePolicy',
            entity_id=str(policy.id),
            old_data=old_data,
            new_data=serializer.data,
            business=biz,
            reason='Enterprise Admin updated global attendance defaults'
        )

        return Response(PolicyResolver.get_attendance_policy(business=biz, user=request.user))


class CentreAttendancePolicyView(views.APIView):
    """
    Returns resolved effective policy for a Center, and allows permitted Managers / Enterprise Admins
    to save Center-specific overrides.
    """
    permission_classes = [IsAuthenticated]

    def get_centre(self, request, pk):
        ctx = get_user_context(request)
        centre = Branch.objects.filter(id=pk).select_related('business').first()
        if not centre:
            raise NotFound('Centre not found.')

        if not ctx['is_superadmin'] and centre.business_id != ctx['business'].id:
            raise PermissionDenied('Cross-tenant centre access forbidden.')

        return centre

    def get(self, request, pk):
        centre = self.get_centre(request, pk)
        policy_data = PolicyResolver.get_attendance_policy(centre=centre, user=request.user)
        return Response(policy_data)

    def put(self, request, pk):
        centre = self.get_centre(request, pk)
        biz = centre.business

        # Verify editing permission: Enterprise Admin or Manager with attendance.manage_policy
        can_edit = False
        if request.user.is_superuser:
            can_edit = True
        else:
            membership = request.user.business_memberships.filter(business=biz, is_active=True).first()
            if membership and membership.role == BusinessRole.BUSINESS_ADMIN:
                can_edit = True
            elif membership and membership.role == BusinessRole.MANAGER:
                ent_policy = AttendancePolicy.objects.filter(business=biz).first()
                if ent_policy and not ent_policy.allow_center_override:
                    raise PermissionDenied('Enterprise Administrator has disabled Center-level policy overrides.')
                can_edit = PermissionService.has_permission(
                    user=request.user,
                    permission_key='attendance.manage_policy',
                    business=biz,
                    centre=centre
                )

        if not can_edit:
            raise PermissionDenied('You do not have permission to modify Centre attendance policy overrides.')

        # Capture old data
        old_override = AttendancePolicyOverride.objects.filter(centre=centre).first()
        old_data = AttendancePolicyOverrideSerializer(old_override).data if old_override else None

        override = PolicyResolver.save_centre_override(centre=centre, override_data=request.data, user=request.user)

        AuditService.log(
            user_or_request=request,
            action='UPDATE_CENTRE_ATTENDANCE_OVERRIDE',
            entity_type='AttendancePolicyOverride',
            entity_id=str(override.id),
            old_data=old_data,
            new_data=AttendancePolicyOverrideSerializer(override).data,
            business=biz,
            reason=f'Updated attendance override for centre {centre.name}'
        )

        return Response(PolicyResolver.get_attendance_policy(centre=centre, user=request.user))


class CentreAttendancePolicyResetView(views.APIView):
    """
    Resets Center override (all fields or a specific field), reverting to Enterprise Defaults.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        centre = Branch.objects.filter(id=pk).select_related('business').first()
        if not centre:
            raise NotFound('Centre not found.')

        ctx = get_user_context(request)
        if not ctx['is_superadmin'] and centre.business_id != ctx['business'].id:
            raise PermissionDenied('Cross-tenant operation forbidden.')

        can_reset = PermissionService.has_permission(
            user=request.user,
            permission_key='attendance.manage_policy',
            business=centre.business,
            centre=centre
        )
        if not can_reset:
            raise PermissionDenied('You do not have permission to reset Centre attendance policy.')

        field_name = request.data.get('field') or request.data.get('field_name')
        PolicyResolver.reset_centre_override(centre=centre, user=request.user, field_name=field_name)

        AuditService.log(
            user_or_request=request,
            action='RESET_CENTRE_ATTENDANCE_OVERRIDE',
            entity_type='AttendancePolicyOverride',
            entity_id=str(centre.id),
            business=centre.business,
            reason=f"Reset {field_name or 'all overrides'} to Enterprise default for centre {centre.name}"
        )

        policy_data = PolicyResolver.get_attendance_policy(centre=centre, user=request.user)
        msg = f"Reset {field_name} to Enterprise default successfully." if field_name else f"Centre '{centre.name}' reset to Enterprise defaults successfully."
        return Response({
            'detail': msg,
            **policy_data
        })
