from rest_framework.permissions import BasePermission
from apps.organization.services.permission_service import PermissionService


class HasFaceEnrollmentPermission(BasePermission):
    """
    Grants permission to enroll, re-enroll, or revoke face biometrics.
    SuperAdmin and Enterprise Admins always have access.
    Managers must have explicit 'employee.face_enrollment' permission granted.
    """
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        if request.user.is_superuser:
            return True

        # Resolve employee context from request
        biz = getattr(request, 'business', None)
        if not biz and hasattr(request.user, 'employee_profiles'):
            emp = request.user.employee_profiles.filter(is_active=True).first()
            if emp:
                biz = emp.business

        # Fallback to query param business if available
        if not biz:
            biz_id = request.data.get('business_id') or request.query_params.get('business_id')
            if biz_id:
                from apps.organization.models import Business
                biz = Business.objects.filter(id=biz_id).first()

        # Check explicit permission via PermissionService
        return PermissionService.has_permission(
            user=request.user,
            permission_key='employee.face_enrollment',
            business=biz
        )
