from typing import List, Dict, Any, Optional
from apps.organization.models import (
    BusinessRole, BusinessMembership, Permission, ManagerAccessControl, PermissionScope
)

# Standard default permissions for Center Managers when not explicitly configured
DEFAULT_MANAGER_PERMITTED = {
    'employees.view',
    'attendance.view',
    'attendance.mark',
    'attendance.edit',
    'attendance.approve_correction',
    'leave.view',
    'leave.approve',
    'leave.reject',
    'documents.view',
    'documents.upload',
    'documents.verify',
    'salary.view',
    'holidays.view',
    'center.settings.view',
    'working_hours.view',
    'reports.view',
    'reports.export',
}

# Standard permissions for Staff/Employees (Self-scoped)
DEFAULT_STAFF_PERMITTED = {
    'attendance.view',
    'attendance.mark',
    'leave.view',
    'leave.apply',
    'documents.view',
    'salary.view',
    'holidays.view',
    'working_hours.view',
}


class PermissionService:
    @staticmethod
    def has_permission(user, permission_key: str, business=None, centre=None, target_employee=None) -> bool:
        """
        Evaluates whether a user has a specific permission in the given business and centre scope.
        Enforces tenant boundaries and managerial scope.
        """
        if not user or not user.is_authenticated:
            return False

        # SuperAdmin has global unrestricted access
        if user.is_superuser:
            return True

        if not business:
            return False

        membership = BusinessMembership.objects.filter(
            user=user,
            business=business,
            is_active=True
        ).first()

        if not membership:
            return False

        # Enterprise / Business Admin has full permissions within the enterprise
        if membership.role == BusinessRole.BUSINESS_ADMIN:
            return True

        # Center Manager: inspect granular ManagerAccessControl grants
        if membership.role == BusinessRole.MANAGER:
            grant = ManagerAccessControl.objects.filter(
                business=business,
                user=user,
                permission__key=permission_key
            ).select_related('permission').first()

            if grant is not None:
                if not grant.is_granted:
                    return False
            else:
                # Fall back to canonical default for managers
                if permission_key not in DEFAULT_MANAGER_PERMITTED:
                    return False

            # Center scope verification: if target employee is provided, ensure they belong to manager's center or direct reports
            if target_employee is not None:
                manager_emp = getattr(user, 'employee_profiles', None)
                manager_emp_obj = manager_emp.filter(business=business).first() if manager_emp else None

                # Manager can access if employee is in the same branch/center OR is a direct report
                same_branch = (
                    manager_emp_obj and manager_emp_obj.branch_id and
                    target_employee.branch_id == manager_emp_obj.branch_id
                )
                direct_report = (
                    manager_emp_obj and target_employee.manager_id == manager_emp_obj.id
                )
                self_access = (manager_emp_obj and target_employee.id == manager_emp_obj.id)

                if not (same_branch or direct_report or self_access):
                    return False

            return True

        # Staff role
        if membership.role == BusinessRole.STAFF:
            if permission_key not in DEFAULT_STAFF_PERMITTED:
                return False
            # Staff can only access their own records
            if target_employee is not None:
                staff_emp = getattr(user, 'employee_profiles', None)
                staff_emp_obj = staff_emp.filter(business=business).first() if staff_emp else None
                if not staff_emp_obj or staff_emp_obj.id != target_employee.id:
                    return False
            return True

        return False

    @staticmethod
    def get_user_permissions(user, business) -> List[str]:
        """
        Returns the resolved list of permission keys granted to this user in this business.
        """
        if not user or not user.is_authenticated:
            return []

        all_keys = list(Permission.objects.values_list('key', flat=True))
        if user.is_superuser:
            return all_keys

        if not business:
            return []

        membership = BusinessMembership.objects.filter(
            user=user,
            business=business,
            is_active=True
        ).first()

        if not membership:
            return []

        if membership.role == BusinessRole.BUSINESS_ADMIN:
            return all_keys

        if membership.role == BusinessRole.MANAGER:
            # Start with default permitted
            effective = set(DEFAULT_MANAGER_PERMITTED)

            # Apply explicit grants and denials
            grants = ManagerAccessControl.objects.filter(
                business=business,
                user=user
            ).select_related('permission')

            for g in grants:
                if g.is_granted:
                    effective.add(g.permission.key)
                else:
                    effective.discard(g.permission.key)

            return sorted(list(effective))

        if membership.role == BusinessRole.STAFF:
            return sorted(list(DEFAULT_STAFF_PERMITTED))

        return []

    @staticmethod
    def get_manager_access_matrix(business, manager_user) -> List[Dict[str, Any]]:
        """
        Returns the full matrix of permissions grouped by module with current granted status
        for Enterprise Admin to review and configure.
        """
        all_perms = Permission.objects.all().order_by('module', 'key')
        grants = {
            g.permission.key: g
            for g in ManagerAccessControl.objects.filter(business=business, user=manager_user).select_related('permission')
        }

        matrix = []
        for p in all_perms:
            grant = grants.get(p.key)
            if grant is not None:
                is_granted = grant.is_granted
                scope = grant.scope
            else:
                is_granted = p.key in DEFAULT_MANAGER_PERMITTED
                scope = p.default_scope

            matrix.append({
                'id': str(p.id),
                'key': p.key,
                'name': p.name,
                'module': p.module,
                'description': p.description,
                'is_granted': is_granted,
                'scope': scope,
                'is_default': grant is None,
            })

        return matrix

    @staticmethod
    def set_manager_permission(business, manager_user, permission_key: str, is_granted: bool, scope=None, granted_by=None):
        """
        Updates or creates an explicit permission grant/denial for a manager.
        """
        perm = Permission.objects.filter(key=permission_key).first()
        if not perm:
            raise ValueError(f"Unknown permission key: {permission_key}")

        scope = scope or perm.default_scope

        control, created = ManagerAccessControl.objects.update_or_create(
            business=business,
            user=manager_user,
            permission=perm,
            defaults={
                'is_granted': is_granted,
                'scope': scope,
                'granted_by': granted_by
            }
        )
        return control
