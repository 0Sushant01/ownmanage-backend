from rest_framework import permissions
from apps.organization.models import BusinessMembership, BusinessRole, Employee


def get_user_context(request):
    """
    Evaluates and caches the user's role and tenant context for the request.
    Returns a dict with:
      - 'role': 'SUPERADMIN' | 'BUSINESS_ADMIN' | 'MANAGER' | 'STAFF' | None
      - 'business': Business instance or None
      - 'membership': BusinessMembership instance or None
      - 'employee': Employee instance or None
      - 'is_superadmin': bool
    """
    if hasattr(request, '_ownmanage_context'):
        return request._ownmanage_context

    user = request.user
    if not user or not user.is_authenticated:
        ctx = {
            'role': None,
            'business': None,
            'membership': None,
            'employee': None,
            'is_superadmin': False,
        }
        request._ownmanage_context = ctx
        return ctx

    # SuperAdmin: platform-wide operator
    if user.is_superuser:
        # Check if SuperAdmin is viewing a specific business via header or query param
        selected_biz_id = request.headers.get('X-Business-ID') or request.query_params.get('business_id')
        business = None
        if selected_biz_id:
            from apps.organization.models import Business
            try:
                business = Business.objects.filter(id=selected_biz_id, is_active=True).first()
            except Exception:
                business = None

        ctx = {
            'role': BusinessRole.SUPERADMIN,
            'business': business,
            'membership': None,
            'employee': None,
            'is_superadmin': True,
        }
        request._ownmanage_context = ctx
        return ctx

    # Broker: platform-level partner role
    if hasattr(user, 'broker_profile') and user.broker_profile.is_active:
        ctx = {
            'role': BusinessRole.BROKER,
            'business': None,
            'membership': None,
            'employee': None,
            'is_superadmin': False,
            'broker': user.broker_profile,
        }
        request._ownmanage_context = ctx
        return ctx

    # Business-level user: find active membership
    selected_biz_id = request.headers.get('X-Business-ID')
    memberships = BusinessMembership.objects.filter(
        user=user,
        is_active=True,
        business__is_active=True
    ).select_related('business')

    membership = None
    if selected_biz_id:
        membership = memberships.filter(business_id=selected_biz_id).first()
    if not membership:
        membership = memberships.first()

    business = membership.business if membership else None
    role = membership.role if membership else None

    # Link employee profile if exists for this user in this business
    employee = None
    if business:
        employee = Employee.objects.filter(
            business=business,
            user=user,
            employment_status__in=['ACTIVE', 'PROBATION']
        ).first()

    ctx = {
        'role': role,
        'business': business,
        'membership': membership,
        'employee': employee,
        'is_superadmin': False,
    }
    request._ownmanage_context = ctx
    return ctx


class IsSuperAdmin(permissions.BasePermission):
    """
    Allows access only to global SuperAdmin users.
    """
    def has_permission(self, request, view):
        ctx = get_user_context(request)
        return bool(ctx['is_superadmin'])


class IsBusinessAdmin(permissions.BasePermission):
    """
    Allows access to SuperAdmin or Business Admin of the current business.
    """
    def has_permission(self, request, view):
        ctx = get_user_context(request)
        if ctx['is_superadmin']:
            return True
        return ctx['role'] == BusinessRole.BUSINESS_ADMIN and ctx['business'] is not None


class IsManagerOrAdmin(permissions.BasePermission):
    """
    Allows access to SuperAdmin, Business Admin, or Manager of the current business.
    """
    def has_permission(self, request, view):
        ctx = get_user_context(request)
        if ctx['is_superadmin']:
            return True
        return ctx['role'] in [BusinessRole.BUSINESS_ADMIN, BusinessRole.MANAGER] and ctx['business'] is not None


class IsTenantMember(permissions.BasePermission):
    """
    Allows access to any authenticated member of a business or SuperAdmin.
    """
    def has_permission(self, request, view):
        ctx = get_user_context(request)
        if ctx['is_superadmin']:
            return True
        return ctx['business'] is not None
