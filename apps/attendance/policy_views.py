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

        # Synchronize inheriting employees in this centre
        from apps.attendance.services.employee_working_hours_service import EmployeeWorkingHoursService
        EmployeeWorkingHoursService.sync_centre_policy_change(centre)

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

        # Synchronize inheriting employees in this centre
        from apps.attendance.services.employee_working_hours_service import EmployeeWorkingHoursService
        EmployeeWorkingHoursService.sync_centre_policy_change(centre)

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


class EmployeeWorkingHoursView(views.APIView):
    """
    Returns and manages employee-specific 7-day working hours schedule,
    with explicit inheritance/override provenance and atomic updating.
    """
    permission_classes = [IsAuthenticated]

    def _get_employee(self, request, pk):
        from apps.organization.models import Employee
        emp = Employee.objects.filter(id=pk).select_related('business', 'branch').first()
        if not emp:
            raise NotFound('Employee not found.')

        ctx = get_user_context(request)
        if not ctx['is_superadmin'] and emp.business_id != ctx['business'].id:
            raise PermissionDenied('Cross-tenant access forbidden.')

        return emp, ctx

    def _can_manage(self, request, emp, ctx):
        if ctx['is_superadmin']:
            return True
        if ctx['role'] == BusinessRole.BUSINESS_ADMIN:
            return True
        if ctx['role'] == BusinessRole.MANAGER:
            mgr_emp = ctx.get('employee')
            if mgr_emp and mgr_emp.branch_id and emp.branch_id != mgr_emp.branch_id:
                return False
            return PermissionService.has_permission(
                user=request.user,
                permission_key='employees.edit',
                business=emp.business,
                centre=emp.branch,
                target_employee=emp
            )
        return False

    def get(self, request, pk):
        emp, ctx = self._get_employee(request, pk)
        from apps.attendance.services.employee_working_hours_service import EmployeeWorkingHoursService
        schedule_data = EmployeeWorkingHoursService.get_or_initialize_schedule(emp)

        centre = emp.branch
        biz = emp.business
        policy_data = PolicyResolver.get_attendance_policy(centre=centre, business=biz, user=request.user)
        effective = policy_data.get('effective', {})

        # Map day numbers to names
        day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

        # Derive employee-specific schedule metrics from the 7-day records
        emp_days = schedule_data.get('days', [])
        enabled_days = [d for d in emp_days if d.get('is_enabled')]
        off_day_names = [d['day_name'] for d in emp_days if not d.get('is_enabled')]
        working_days_count = len(enabled_days) if emp_days else effective.get('working_days', 5)
        weekly_off_names = off_day_names if emp_days else [day_names[i] for i in (effective.get('weekly_off_days') or [6]) if 0 <= i < 7]

        # Shift timings from the first active working day, or policy fallback
        first_enabled = enabled_days[0] if enabled_days else None
        shift_timings = {
            'office_start': (first_enabled.get('start_time') if first_enabled else None) or effective.get('office_start', '09:00'),
            'office_end': (first_enabled.get('end_time') if first_enabled else None) or effective.get('office_end', '18:00'),
            'break_start': (first_enabled.get('break_start') if first_enabled else None) or effective.get('break_start', '13:00'),
            'break_end': (first_enabled.get('break_end') if first_enabled else None) or effective.get('break_end', '14:00'),
        }

        can_edit = self._can_manage(request, emp, ctx)

        return Response({
            **schedule_data,
            'can_edit': can_edit,
            'shift_timings': shift_timings,
            'rules': {
                'grace_period_minutes': effective.get('grace_period_minutes', 15),
                'minimum_present_minutes': effective.get('minimum_present_minutes', 480),
                'minimum_half_day_minutes': effective.get('minimum_half_day_minutes', 240),
                'late_threshold_minutes': effective.get('late_threshold_minutes', 30),
                'early_checkout_threshold_minutes': effective.get('early_checkout_threshold_minutes', 30),
                'working_days_per_week': working_days_count,
                'weekly_off_days': weekly_off_names,
                'ot_enabled': effective.get('ot_enabled', False),
                'ot_grace_minutes': effective.get('ot_grace_minutes', 30),
                'max_daily_ot_minutes': effective.get('max_daily_ot_minutes', 240),
            },
            'verification_methods': {
                'normal_punch': effective.get('allow_normal_punch', True),
                'gps': effective.get('allow_gps', True),
                'geofencing': effective.get('allow_geofencing', False),
                'qr': effective.get('allow_qr', False),
                'face_recognition': effective.get('allow_face_recognition', False),
                'biometric': effective.get('allow_biometric', False),
            },
            'effective': effective,
        })

    def put(self, request, pk):
        emp, ctx = self._get_employee(request, pk)
        if not self._can_manage(request, emp, ctx):
            raise PermissionDenied('You do not have permission to modify employee working hours.')

        from apps.attendance.services.employee_working_hours_service import EmployeeWorkingHoursService

        action = request.data.get('action')
        is_override = request.data.get('is_override')

        if action == 'reset' or is_override is False:
            EmployeeWorkingHoursService.reset_schedule_to_centre(emp, user=request.user)
        elif 'days' in request.data:
            EmployeeWorkingHoursService.update_employee_schedule(
                employee=emp,
                days_data=request.data['days'],
                user=request.user,
                reason=request.data.get('reason')
            )
        else:
            raise ValidationError({'detail': 'Either days list or reset action is required.'})

        return self.get(request, pk)

    def patch(self, request, pk):
        return self.put(request, pk)


class EmployeeWorkingHoursResetView(views.APIView):
    """
    Explicit endpoint to reset an employee's working hours to inherit the centre's effective policy.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        from apps.organization.models import Employee
        emp = Employee.objects.filter(id=pk).select_related('business', 'branch').first()
        if not emp:
            raise NotFound('Employee not found.')

        ctx = get_user_context(request)
        if not ctx['is_superadmin'] and emp.business_id != ctx['business'].id:
            raise PermissionDenied('Cross-tenant access forbidden.')

        can_edit = False
        if ctx['is_superadmin'] or ctx['role'] == BusinessRole.BUSINESS_ADMIN:
            can_edit = True
        elif ctx['role'] == BusinessRole.MANAGER:
            mgr_emp = ctx.get('employee')
            if not (mgr_emp and mgr_emp.branch_id and emp.branch_id != mgr_emp.branch_id):
                can_edit = PermissionService.has_permission(
                    user=request.user,
                    permission_key='employees.edit',
                    business=emp.business,
                    centre=emp.branch,
                    target_employee=emp
                )

        if not can_edit:
            raise PermissionDenied('You do not have permission to reset employee working hours.')

        from apps.attendance.services.employee_working_hours_service import EmployeeWorkingHoursService
        EmployeeWorkingHoursService.reset_schedule_to_centre(emp, user=request.user)

        resp = EmployeeWorkingHoursView().get(request, pk)
        resp.data['detail'] = f"Working hours reset to centre defaults for {emp.full_name}."
        return resp
