from datetime import date
from django.utils import timezone
from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, NotFound, ValidationError

from apps.core.permissions import get_user_context
from apps.organization.models import Business, BusinessRole, Employee, Branch
from apps.payroll.models import (
    SalaryRevision, PayrollRun, PayrollRunStatus, PayrollStatus, Payroll, PayrollException
)
from apps.payroll.serializers import (
    SalaryRevisionSerializer, PayrollRunSerializer, PayrollSerializer, PayrollExceptionSerializer
)
from apps.payroll.services.payroll_calculation_service import PayrollCalculationService
from apps.organization.services.permission_service import PermissionService
from apps.core.services.audit_service import AuditService


class EmployeeSalaryRevisionListCreateView(views.APIView):
    """
    Manages immutable employee salary revisions.
    """
    permission_classes = [IsAuthenticated]

    def get_employee(self, request, pk):
        emp = Employee.objects.filter(id=pk).select_related('business').first()
        if not emp:
            raise NotFound('Employee not found.')

        ctx = get_user_context(request)
        if not ctx['is_superadmin'] and emp.business_id != ctx['business'].id:
            raise PermissionDenied('Cross-tenant employee access forbidden.')

        if not PermissionService.has_permission(request.user, 'salary.view', business=emp.business, target_employee=emp):
            raise PermissionDenied('You do not have permission to view salary records.')

        return emp

    def get(self, request, pk):
        emp = self.get_employee(request, pk)
        revisions = SalaryRevision.objects.filter(employee=emp).order_by('-effective_from', '-created_at')
        return Response(SalaryRevisionSerializer(revisions, many=True).data)

    def post(self, request, pk):
        emp = self.get_employee(request, pk)
        if not PermissionService.has_permission(request.user, 'salary.edit', business=emp.business, target_employee=emp):
            raise PermissionDenied('You do not have permission to create salary revisions.')

        serializer = SalaryRevisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        eff_from = serializer.validated_data['effective_from']
        confirm_backdated = request.data.get('confirm_backdated', False)

        # Check if backdated against finalized payroll
        from apps.payroll.models import Payroll
        affected_payrolls = list(Payroll.objects.filter(
            employee=emp,
            status=PayrollStatus.PAID,
            period_end__gte=eff_from
        ).values('id', 'period_start', 'period_end', 'net_amount'))

        if affected_payrolls and not confirm_backdated:
            return Response({
                'warning': 'This change may affect previously generated payroll.',
                'requires_confirmation': True,
                'affected_periods': [
                    f"{p['period_start']} to {p['period_end']} (Net: {p['net_amount']})"
                    for p in affected_payrolls
                ],
                'detail': 'Historical finalized payroll will NOT be silently recalculated. Please confirm to proceed.'
            }, status=status.HTTP_409_CONFLICT)

        # Remove duplicate revisions on the same effective_from date
        SalaryRevision.objects.filter(employee=emp, effective_from=eff_from).delete()

        # Close out previous active revisions whose effective_from is before eff_from
        prior_revisions = SalaryRevision.objects.filter(
            employee=emp,
            effective_to__isnull=True,
            effective_from__lt=eff_from
        )
        prev = prior_revisions.first()
        for p_rev in prior_revisions:
            p_rev.effective_to = eff_from
            p_rev.save(update_fields=['effective_to'])

        salary_unit = serializer.validated_data.get('salary_unit') or request.data.get('salary_unit')
        if not salary_unit:
            from apps.payroll.services.payroll_schedule_service import PayrollScheduleService
            sched = PayrollScheduleService.resolve_schedule(employee=emp)
            salary_unit = sched['effective_config'].get('generation_type', 'MONTHLY')

        revision = serializer.save(
            business=emp.business,
            employee=emp,
            revised_by=request.user,
            salary_unit=salary_unit
        )

        from apps.organization.models import EmployeeActivityLog
        prev_salary_val = str(prev.basic_salary) if prev else "None"
        new_salary_val = str(revision.basic_salary)
        EmployeeActivityLog.objects.create(
            business=emp.business,
            employee=emp,
            activity_type='SALARY_REVISION',
            description=f"Salary revised: {revision.currency} {prev_salary_val} → {new_salary_val}",
            old_value={'basic_salary': prev_salary_val},
            new_value={'basic_salary': new_salary_val, 'effective_from': str(revision.effective_from), 'reason': revision.reason},
            performed_by=request.user
        )

        AuditService.log(
            user_or_request=request,
            action='CREATE_SALARY_REVISION',
            entity_type='SalaryRevision',
            entity_id=str(revision.id),
            new_data={
                'basic_salary': float(revision.basic_salary),
                'effective_from': str(revision.effective_from),
                'reason': revision.reason,
                'confirmed_backdated': bool(affected_payrolls and confirm_backdated)
            },
            business=emp.business,
            reason=f'Created salary revision for {emp.full_name}: {revision.currency} {revision.basic_salary}'
        )

        return Response(SalaryRevisionSerializer(revision).data, status=status.HTTP_201_CREATED)


class EmployeeSalaryRevisionDetailView(views.APIView):
    """
    Retrieve, update, or delete an employee's salary revision.
    Enforces business rules:
    - Only Upcoming and Current salary revisions may be updated or deleted.
    - Historical revisions are strictly immutable and cannot be updated or deleted.
    """
    permission_classes = [IsAuthenticated]

    def get_employee(self, request, pk):
        emp = Employee.objects.filter(id=pk).select_related('business').first()
        if not emp:
            raise NotFound('Employee not found.')

        ctx = get_user_context(request)
        if not ctx['is_superadmin'] and emp.business_id != ctx['business'].id:
            raise PermissionDenied('Cross-tenant employee access forbidden.')

        return emp

    def get_revision_and_status(self, emp, rev_pk):
        revision = SalaryRevision.objects.filter(id=rev_pk, employee=emp).select_related('revised_by').first()
        if not revision:
            raise NotFound('Salary revision not found.')

        today = timezone.localdate() if hasattr(timezone, 'localdate') else timezone.now().date()
        all_revisions = list(SalaryRevision.objects.filter(employee=emp).order_by('-effective_from', '-created_at'))

        current_rev = None
        for r in all_revisions:
            if r.effective_from <= today and (r.effective_to is None or r.effective_to >= today):
                current_rev = r
                break

        is_upcoming = (revision.effective_from > today)
        is_current = (current_rev is not None and current_rev.id == revision.id)
        is_historical = not is_upcoming and not is_current

        return revision, {
            'is_upcoming': is_upcoming,
            'is_current': is_current,
            'is_historical': is_historical
        }

    def get(self, request, pk, rev_pk):
        emp = self.get_employee(request, pk)
        if not PermissionService.has_permission(request.user, 'salary.view', business=emp.business, target_employee=emp):
            raise PermissionDenied('You do not have permission to view salary records.')

        revision, status_info = self.get_revision_and_status(emp, rev_pk)
        data = SalaryRevisionSerializer(revision).data
        data.update(status_info)
        return Response(data)

    def put(self, request, pk, rev_pk):
        return self.patch(request, pk, rev_pk)

    def patch(self, request, pk, rev_pk):
        emp = self.get_employee(request, pk)
        if not PermissionService.has_permission(request.user, 'salary.edit', business=emp.business, target_employee=emp):
            raise PermissionDenied('You do not have permission to edit salary revisions.')

        revision, status_info = self.get_revision_and_status(emp, rev_pk)

        if status_info['is_historical']:
            return Response(
                {'detail': 'Historical salary revisions cannot be modified for audit integrity.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        data = request.data
        old_val = str(revision.basic_salary)

        if 'basic_salary' in data and data['basic_salary'] is not None:
            revision.basic_salary = data['basic_salary']
        if 'hourly_rate' in data and data['hourly_rate'] is not None:
            revision.hourly_rate = data['hourly_rate']
        if 'ot_rate' in data and data['ot_rate'] is not None:
            revision.ot_rate = data['ot_rate']
        if 'reason' in data and data['reason'] is not None:
            revision.reason = data['reason']
        if 'salary_unit' in data and data['salary_unit']:
            revision.salary_unit = data['salary_unit']

        if 'effective_from' in data and data['effective_from']:
            from datetime import datetime as dt
            new_eff_from = dt.strptime(str(data['effective_from']), '%Y-%m-%d').date() if isinstance(data['effective_from'], str) else data['effective_from']
            if str(revision.effective_from) != str(new_eff_from):
                SalaryRevision.objects.filter(employee=emp, effective_from=new_eff_from).exclude(id=revision.id).delete()
                revision.effective_from = new_eff_from

        revision.revised_by = request.user
        revision.save()

        prior_revisions = SalaryRevision.objects.filter(
            employee=emp,
            effective_from__lt=revision.effective_from
        ).exclude(id=revision.id).order_by('-effective_from')
        if prior_revisions.exists():
            prior = prior_revisions.first()
            prior.effective_to = revision.effective_from
            prior.save(update_fields=['effective_to'])

        from apps.organization.models import EmployeeActivityLog
        EmployeeActivityLog.objects.create(
            business=emp.business,
            employee=emp,
            activity_type='SALARY_REVISION_EDIT',
            description=f"Salary revision updated: {revision.currency} {old_val} → {revision.basic_salary}",
            old_value={'basic_salary': old_val},
            new_value={'basic_salary': str(revision.basic_salary), 'effective_from': str(revision.effective_from), 'reason': revision.reason},
            performed_by=request.user
        )

        AuditService.log(
            user_or_request=request,
            action='UPDATE_SALARY_REVISION',
            entity_type='SalaryRevision',
            entity_id=str(revision.id),
            new_data={
                'basic_salary': float(revision.basic_salary),
                'effective_from': str(revision.effective_from),
                'reason': revision.reason,
            },
            business=emp.business,
            reason=f'Updated salary revision for {emp.full_name}: {revision.currency} {revision.basic_salary}'
        )

        return Response(SalaryRevisionSerializer(revision).data)

    def delete(self, request, pk, rev_pk):
        emp = self.get_employee(request, pk)
        if not PermissionService.has_permission(request.user, 'salary.edit', business=emp.business, target_employee=emp):
            raise PermissionDenied('You do not have permission to delete salary revisions.')

        revision, status_info = self.get_revision_and_status(emp, rev_pk)

        if status_info['is_historical']:
            return Response(
                {'detail': 'Historical salary revisions cannot be deleted for audit and compliance integrity.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        deleted_val = str(revision.basic_salary)
        deleted_eff_from = revision.effective_from

        prior_rev = SalaryRevision.objects.filter(
            employee=emp,
            effective_from__lt=deleted_eff_from
        ).exclude(id=revision.id).order_by('-effective_from').first()

        next_rev = SalaryRevision.objects.filter(
            employee=emp,
            effective_from__gt=deleted_eff_from
        ).exclude(id=revision.id).order_by('effective_from').first()

        revision.delete()

        if prior_rev:
            prior_rev.effective_to = next_rev.effective_from if next_rev else None
            prior_rev.save(update_fields=['effective_to'])

        from apps.organization.models import EmployeeActivityLog
        EmployeeActivityLog.objects.create(
            business=emp.business,
            employee=emp,
            activity_type='SALARY_REVISION_DELETE',
            description=f"Salary revision deleted: {deleted_val} (Effective: {deleted_eff_from})",
            old_value={'basic_salary': deleted_val, 'effective_from': str(deleted_eff_from)},
            new_value={},
            performed_by=request.user
        )

        AuditService.log(
            user_or_request=request,
            action='DELETE_SALARY_REVISION',
            entity_type='SalaryRevision',
            entity_id=str(rev_pk),
            new_data={'deleted': True},
            business=emp.business,
            reason=f'Deleted salary revision for {emp.full_name}: {deleted_val} (Effective: {deleted_eff_from})'
        )

        return Response({'detail': 'Salary revision deleted successfully.'}, status=status.HTTP_200_OK)


class EmployeeSalaryComparisonView(views.APIView):
    """
    Compares consecutive salary revisions for an employee:
    Old vs New, Absolute Difference, Percentage Change, Reason, Changed By.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        emp = Employee.objects.filter(id=pk).select_related('business').first()
        if not emp:
            raise NotFound('Employee not found.')

        ctx = get_user_context(request)
        if not ctx['is_superadmin'] and emp.business_id != ctx['business'].id:
            raise PermissionDenied('Cross-tenant employee access forbidden.')

        rev_from_id = request.query_params.get('revision_from')
        rev_to_id = request.query_params.get('revision_to')
        if rev_from_id and rev_to_id:
            rev_from = SalaryRevision.objects.filter(id=rev_from_id, employee=emp).select_related('revised_by').first()
            rev_to = SalaryRevision.objects.filter(id=rev_to_id, employee=emp).select_related('revised_by').first()
            if rev_from and rev_to:
                from_val = float(rev_from.basic_salary)
                to_val = float(rev_to.basic_salary)
                diff = to_val - from_val
                pct = round((diff / from_val * 100), 2) if from_val > 0 else 0.0
                return Response({
                    'employee_id': str(emp.id),
                    'employee_name': emp.full_name,
                    'comparisons': [{
                        'revision_id': str(rev_to.id),
                        'effective_from': str(rev_to.effective_from),
                        'effective_to': str(rev_to.effective_to) if rev_to.effective_to else 'Present',
                        'new_salary': to_val,
                        'previous_salary': from_val,
                        'difference': diff,
                        'percentage': pct,
                        'currency': rev_to.currency,
                        'reason': rev_to.reason or 'Regular revision',
                        'changed_by': rev_to.revised_by.get_full_name() if rev_to.revised_by else 'System Admin',
                        'is_current': rev_to.effective_to is None,
                    }]
                })

        revisions = list(SalaryRevision.objects.filter(employee=emp).select_related('revised_by').order_by('-effective_from', '-created_at'))
        if not revisions:
            return Response({
                'employee_id': str(emp.id),
                'employee_name': emp.full_name,
                'comparisons': []
            })

        today = timezone.localdate() if hasattr(timezone, 'localdate') else timezone.now().date()
        current_rev = None
        for r in revisions:
            if r.effective_from <= today and (r.effective_to is None or r.effective_to >= today):
                current_rev = r
                break
        current_rev_id = str(current_rev.id) if current_rev else None

        unit_map = {'MONTHLY': '/ month', 'WEEKLY': '/ week', 'DAILY': '/ day'}
        comparisons = []
        for i in range(len(revisions)):
            curr = revisions[i]
            prev = revisions[i + 1] if i + 1 < len(revisions) else None

            curr_val = float(curr.basic_salary)
            prev_val = float(prev.basic_salary) if prev else 0.0
            diff = curr_val - prev_val
            pct = round((diff / prev_val * 100), 2) if prev_val > 0 else 0.0

            curr_unit = getattr(curr, 'salary_unit', 'MONTHLY') or 'MONTHLY'
            prev_unit = (getattr(prev, 'salary_unit', 'MONTHLY') or 'MONTHLY') if prev else None

            is_upcoming = (curr.effective_from > today)
            is_current = (str(curr.id) == current_rev_id)

            comparisons.append({
                'revision_id': str(curr.id),
                'effective_from': str(curr.effective_from),
                'effective_to': str(curr.effective_to) if curr.effective_to else 'Present',
                'new_salary': curr_val,
                'salary_unit': curr_unit,
                'salary_unit_display': unit_map.get(curr_unit, '/ month'),
                'previous_salary': prev_val if prev else None,
                'previous_salary_unit': prev_unit,
                'previous_salary_unit_display': unit_map.get(prev_unit, '/ month') if prev_unit else None,
                'difference': diff if prev else 0.0,
                'percentage': pct if prev else 0.0,
                'currency': curr.currency,
                'reason': curr.reason or 'Regular revision',
                'changed_by': curr.revised_by.get_full_name() if curr.revised_by else 'System Admin',
                'is_current': is_current,
                'is_upcoming': is_upcoming,
                'status': 'UPCOMING' if is_upcoming else ('CURRENT' if is_current else 'HISTORICAL'),
            })

        return Response({
            'employee_id': str(emp.id),
            'employee_name': emp.full_name,
            'comparisons': comparisons
        })


class EmployeeCompensationItemListCreateView(views.APIView):
    """
    Itemized compensation components (Earnings, Bonuses, Allowances, Deductions) for an employee.
    Resolves configuration hierarchy: Enterprise Default -> Centre Override -> Employee Override.
    """
    permission_classes = [IsAuthenticated]

    def get_employee(self, request, pk):
        emp = Employee.objects.filter(id=pk).select_related('business', 'branch').first()
        if not emp:
            raise NotFound('Employee not found.')

        ctx = get_user_context(request)
        if not ctx['is_superadmin'] and emp.business_id != ctx['business'].id:
            raise PermissionDenied('Cross-tenant employee access forbidden.')

        return emp

    def get(self, request, pk):
        emp = self.get_employee(request, pk)
        from apps.payroll.services.compensation_resolver import CompensationResolver
        from apps.payroll.serializers import EmployeeCompensationItemSerializer

        active_only = request.query_params.get('active_only')
        include_inactive = not (active_only and active_only.lower() in ['true', '1'])
        resolved = CompensationResolver.resolve_for_employee(emp, include_inactive=include_inactive)

        component_type = request.query_params.get('type')
        if component_type:
            resolved = [r for r in resolved if r['component_type'] == component_type.upper()]

        is_daily = CompensationResolver.is_daily_wage_employee(emp)
        for r in resolved:
            r['is_daily_wage'] = is_daily
            r['daily_wage_notice'] = 'Itemized compensation components are not applicable to daily-wage payroll.' if is_daily else ''

        response = Response(resolved)
        response['X-Is-Daily-Wage'] = 'true' if is_daily else 'false'
        if is_daily:
            response['X-Daily-Wage-Notice'] = 'Itemized compensation components are not applicable to daily-wage payroll.'
        return response

    def post(self, request, pk):
        emp = self.get_employee(request, pk)
        if not PermissionService.has_permission(request.user, 'salary.edit', business=emp.business, centre=emp.branch, target_employee=emp):
            raise PermissionDenied('You do not have permission to manage employee compensation items.')

        from apps.payroll.serializers import EmployeeCompensationItemSerializer
        from apps.payroll.models import EmployeeCompensationItem, ScheduleConfigScope
        from apps.organization.models import EmployeeActivityLog

        data = dict(request.data)
        # Normalize frequency / recurrence
        freq = data.get('frequency', 'RECURRING')
        if data.get('recurrence_type') == 'ONE_TIME':
            freq = 'ONE_TIME'
        elif data.get('recurrence_frequency') == 'WEEKLY':
            freq = 'WEEKLY'
        elif data.get('recurrence_frequency') == 'MONTHLY':
            freq = 'MONTHLY'
        data['frequency'] = freq

        serializer = EmployeeCompensationItemSerializer(data=data)
        serializer.is_valid(raise_exception=True)

        # Check if overriding an existing Enterprise or Centre default
        parent_item_id = data.get('parent_item')
        parent_item = None
        is_override = False
        if parent_item_id:
            parent_item = EmployeeCompensationItem.objects.filter(id=parent_item_id, business=emp.business).first()
            if parent_item:
                is_override = True
        else:
            # Check by name and component type in parent scopes
            parent_match = EmployeeCompensationItem.objects.filter(
                business=emp.business,
                scope__in=[ScheduleConfigScope.ENTERPRISE, ScheduleConfigScope.CENTRE],
                name__iexact=serializer.validated_data['name'].strip(),
                component_type=serializer.validated_data.get('component_type', 'EARNING')
            ).first()
            if parent_match:
                parent_item = parent_match
                is_override = True

        item = serializer.save(
            business=emp.business,
            centre=emp.branch,
            employee=emp,
            scope=ScheduleConfigScope.EMPLOYEE,
            parent_item=parent_item,
            is_override=is_override,
            created_by=request.user
        )

        EmployeeActivityLog.objects.create(
            business=emp.business,
            employee=emp,
            activity_type='COMPENSATION_ITEM_ADDED',
            description=f"Added {item.component_type}: {item.name} ({item.amount}) [Affect Payroll: {'ON' if item.affects_payroll else 'OFF'}]",
            new_value=EmployeeCompensationItemSerializer(item).data,
            performed_by=request.user
        )

        AuditService.log(
            user_or_request=request,
            action='ADD_COMPENSATION_ITEM',
            entity_type='EmployeeCompensationItem',
            entity_id=str(item.id),
            new_data=EmployeeCompensationItemSerializer(item).data,
            business=emp.business,
            reason=f"Added {item.component_type} component '{item.name}' for {emp.full_name}"
        )

        return Response(EmployeeCompensationItemSerializer(item).data, status=status.HTTP_201_CREATED)


class EmployeeCompensationItemDetailView(views.APIView):
    """
    Manage single compensation item (Update, Toggle Affect Payroll, or Soft-Deactivate).
    Supports field-level employee overrides on inherited parent defaults.
    """
    permission_classes = [IsAuthenticated]

    def get_context_and_item(self, request, pk, comp_pk):
        from apps.payroll.models import EmployeeCompensationItem
        emp = Employee.objects.filter(id=pk).select_related('business', 'branch').first()
        if not emp:
            raise NotFound('Employee not found.')

        ctx = get_user_context(request)
        if not ctx['is_superadmin'] and emp.business_id != ctx['business'].id:
            raise PermissionDenied('Cross-tenant access forbidden.')

        item = EmployeeCompensationItem.objects.filter(id=comp_pk, business_id=emp.business_id).select_related('business', 'employee', 'centre').first()
        if not item:
            raise NotFound('Compensation component not found.')

        return emp, item

    def patch(self, request, pk, comp_pk):
        emp, item = self.get_context_and_item(request, pk, comp_pk)
        if not PermissionService.has_permission(request.user, 'salary.edit', business=emp.business, centre=emp.branch, target_employee=emp):
            raise PermissionDenied('Permission denied to update compensation item.')

        from apps.payroll.models import EmployeeCompensationItem, ScheduleConfigScope
        from apps.payroll.serializers import EmployeeCompensationItemSerializer
        from apps.organization.models import EmployeeActivityLog

        # If modifying an inherited Enterprise or Centre component, create an Employee-level override
        if item.scope in [ScheduleConfigScope.ENTERPRISE, ScheduleConfigScope.CENTRE]:
            override_data = {
                'name': item.name,
                'component_type': item.component_type,
                'calculation_type': item.calculation_type,
                'frequency': item.frequency,
                'amount': item.amount,
                'effective_from': item.effective_from,
                'effective_to': item.effective_to,
                'affects_payroll': item.affects_payroll,
                'is_active': item.is_active,
                'reason': f"Employee override for {item.name}",
            }
            override_data.update(request.data)
            serializer = EmployeeCompensationItemSerializer(data=override_data)
            serializer.is_valid(raise_exception=True)
            updated = serializer.save(
                business=emp.business,
                centre=emp.branch,
                employee=emp,
                scope=ScheduleConfigScope.EMPLOYEE,
                parent_item=item,
                is_override=True,
                created_by=request.user
            )
            action_desc = f"Created employee override for inherited component '{item.name}'"
        else:
            # Updating employee's own record
            old_data = EmployeeCompensationItemSerializer(item).data
            serializer = EmployeeCompensationItemSerializer(item, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            updated = serializer.save()
            action_desc = f"Updated compensation item '{item.name}' for {emp.full_name}"

        EmployeeActivityLog.objects.create(
            business=emp.business,
            employee=emp,
            activity_type='COMPENSATION_ITEM_UPDATED',
            description=action_desc,
            new_value=EmployeeCompensationItemSerializer(updated).data,
            performed_by=request.user
        )

        AuditService.log(
            user_or_request=request,
            action='UPDATE_COMPENSATION_ITEM',
            entity_type='EmployeeCompensationItem',
            entity_id=str(updated.id),
            new_data=EmployeeCompensationItemSerializer(updated).data,
            business=emp.business,
            reason=action_desc
        )

        return Response(EmployeeCompensationItemSerializer(updated).data)

    def delete(self, request, pk, comp_pk):
        """
        Soft deactivates or cancels the compensation component without altering historical payroll.
        If an inherited component is deactivated for an employee, creates an employee-level override
        with is_active=False so centre defaults do not re-enable it for this employee.
        """
        emp, item = self.get_context_and_item(request, pk, comp_pk)
        if not PermissionService.has_permission(request.user, 'salary.edit', business=emp.business, centre=emp.branch, target_employee=emp):
            raise PermissionDenied('Permission denied to deactivate compensation item.')

        from apps.payroll.models import EmployeeCompensationItem, ScheduleConfigScope
        from apps.payroll.serializers import EmployeeCompensationItemSerializer
        from apps.organization.models import EmployeeActivityLog

        if item.scope in [ScheduleConfigScope.ENTERPRISE, ScheduleConfigScope.CENTRE]:
            # Explicitly disable inherited component for this employee
            target_item = EmployeeCompensationItem.objects.create(
                business=emp.business,
                centre=emp.branch,
                employee=emp,
                scope=ScheduleConfigScope.EMPLOYEE,
                parent_item=item,
                is_override=True,
                name=item.name,
                component_type=item.component_type,
                calculation_type=item.calculation_type,
                frequency=item.frequency,
                amount=item.amount,
                effective_from=item.effective_from,
                effective_to=date.today(),
                affects_payroll=False,
                is_active=False,
                reason=f"Explicitly deactivated inherited component '{item.name}' for {emp.full_name}",
                created_by=request.user
            )
            desc = f"Explicitly disabled inherited {item.component_type}: {item.name}"
        else:
            target_item = item
            target_item.is_active = False
            target_item.effective_to = date.today()
            target_item.save(update_fields=['is_active', 'effective_to', 'updated_at'])
            desc = f"Deactivated {item.component_type}: {item.name}"

        EmployeeActivityLog.objects.create(
            business=emp.business,
            employee=emp,
            activity_type='COMPENSATION_ITEM_DEACTIVATED',
            description=desc,
            new_value={'is_active': False, 'effective_to': str(target_item.effective_to)},
            performed_by=request.user
        )

        AuditService.log(
            user_or_request=request,
            action='DEACTIVATE_COMPENSATION_ITEM',
            entity_type='EmployeeCompensationItem',
            entity_id=str(target_item.id),
            business=emp.business,
            reason=f"Deactivated compensation component '{target_item.name}' for {emp.full_name}"
        )

        return Response({
            'detail': f"Compensation component '{item.name}' deactivated successfully.",
            'item': EmployeeCompensationItemSerializer(target_item).data
        })


class CentreCompensationItemListCreateView(views.APIView):
    """
    Manage Centre-level default compensation components (Centre Overrides).
    """
    permission_classes = [IsAuthenticated]

    def get_centre(self, request, pk):
        centre = Branch.objects.filter(id=pk).select_related('business').first()
        if not centre:
            raise NotFound('Centre not found.')
        ctx = get_user_context(request)
        if not ctx['is_superadmin'] and centre.business_id != ctx['business'].id:
            raise PermissionDenied('Cross-tenant access forbidden.')
        return centre

    def get(self, request, pk):
        centre = self.get_centre(request, pk)
        from apps.payroll.models import EmployeeCompensationItem, ScheduleConfigScope
        from apps.payroll.serializers import EmployeeCompensationItemSerializer
        qs = EmployeeCompensationItem.objects.filter(
            business=centre.business,
            centre=centre,
            scope=ScheduleConfigScope.CENTRE
        ).select_related('created_by')
        return Response(EmployeeCompensationItemSerializer(qs, many=True).data)

    def post(self, request, pk):
        centre = self.get_centre(request, pk)
        if not PermissionService.has_permission(request.user, 'salary.edit', business=centre.business, centre=centre):
            raise PermissionDenied('Permission denied to configure centre compensation items.')
        from apps.payroll.models import ScheduleConfigScope
        from apps.payroll.serializers import EmployeeCompensationItemSerializer
        serializer = EmployeeCompensationItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = serializer.save(
            business=centre.business,
            centre=centre,
            employee=None,
            scope=ScheduleConfigScope.CENTRE,
            created_by=request.user
        )
        return Response(EmployeeCompensationItemSerializer(item).data, status=status.HTTP_201_CREATED)


class EnterpriseCompensationItemListCreateView(views.APIView):
    """
    Manage Enterprise-level default compensation components.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        from apps.payroll.models import EmployeeCompensationItem, ScheduleConfigScope
        from apps.payroll.serializers import EmployeeCompensationItemSerializer
        qs = EmployeeCompensationItem.objects.filter(
            business=biz,
            scope=ScheduleConfigScope.ENTERPRISE
        ).select_related('created_by')
        return Response(EmployeeCompensationItemSerializer(qs, many=True).data)

    def post(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if not PermissionService.has_permission(request.user, 'salary.edit', business=biz):
            raise PermissionDenied('Permission denied to configure enterprise compensation items.')
        from apps.payroll.models import ScheduleConfigScope
        from apps.payroll.serializers import EmployeeCompensationItemSerializer
        serializer = EmployeeCompensationItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = serializer.save(
            business=biz,
            centre=None,
            employee=None,
            scope=ScheduleConfigScope.ENTERPRISE,
            created_by=request.user
        )
        return Response(EmployeeCompensationItemSerializer(item).data, status=status.HTTP_201_CREATED)


class PayrollRunListCreateView(views.APIView):
    """
    Lists and initiates batched enterprise/centre payroll calculations.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if not biz:
            return Response([])

        if not PermissionService.has_permission(request.user, 'payroll.view', business=biz):
            raise PermissionDenied('You do not have permission to view payroll runs.')

        qs = PayrollRun.objects.filter(business=biz)
        centre_id = request.query_params.get('centre_id')
        if centre_id and centre_id not in ['all', 'ALL', 'null', '']:
            branch_obj = Branch.resolve_branch(centre_id, business=biz)
            if branch_obj:
                qs = qs.filter(centre_id=branch_obj.id)
            else:
                qs = qs.none()

        status_param = request.query_params.get('status')
        if status_param and status_param not in ['all', 'ALL', 'null', '']:
            qs = qs.filter(status=status_param.upper())

        freq_param = request.query_params.get('pay_frequency') or request.query_params.get('frequency')
        if freq_param and freq_param not in ['all', 'ALL', 'null', '']:
            qs = qs.filter(pay_frequency=freq_param)

        return Response(PayrollRunSerializer(qs.select_related('centre', 'approved_by')[:100], many=True).data)

    def post(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if not biz:
            raise PermissionDenied('No active business context.')

        if not PermissionService.has_permission(request.user, 'payroll.calculate', business=biz):
            raise PermissionDenied('You do not have permission to trigger payroll calculation.')

        p_start_str = request.data.get('period_start')
        p_end_str = request.data.get('period_end')
        if not p_start_str or not p_end_str:
            raise ValidationError({'detail': 'period_start and period_end dates are required (YYYY-MM-DD).'})

        from datetime import datetime
        try:
            period_start = datetime.strptime(p_start_str, '%Y-%m-%d').date()
            period_end = datetime.strptime(p_end_str, '%Y-%m-%d').date()
        except ValueError:
            raise ValidationError({'detail': 'Invalid date format. Expected YYYY-MM-DD.'})

        if period_end < period_start:
            raise ValidationError({'detail': 'period_end must be greater than or equal to period_start.'})

        centre = None
        centre_id = request.data.get('centre_id')
        if centre_id and centre_id not in ['all', 'ALL', 'null', '']:
            centre = Branch.resolve_branch(centre_id, business=biz)

        run = PayrollCalculationService.run_batch_payroll(
            business=biz,
            period_start=period_start,
            period_end=period_end,
            centre=centre,
            approved_by=request.user
        )

        AuditService.log(
            user_or_request=request,
            action='RUN_BATCH_PAYROLL',
            entity_type='PayrollRun',
            entity_id=str(run.id),
            new_data={
                'period_start': str(period_start),
                'period_end': str(period_end),
                'total_employees': run.total_employees,
                'total_net': float(run.total_net)
            },
            business=biz,
            reason=f'Calculated payroll run for period {period_start} to {period_end}'
        )

        return Response(PayrollRunSerializer(run).data, status=status.HTTP_201_CREATED)


class PayrollRunApproveView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        ctx = get_user_context(request)
        biz = ctx['business']
        run = PayrollRun.objects.filter(id=pk, business=biz).first()
        if not run:
            raise NotFound('Payroll run not found.')

        if not PermissionService.has_permission(request.user, 'payroll.approve', business=biz):
            raise PermissionDenied('You do not have permission to approve payroll runs.')

        run.status = PayrollRunStatus.APPROVED
        run.approved_by = request.user
        run.save(update_fields=['status', 'approved_by', 'updated_at'])

        AuditService.log(
            user_or_request=request,
            action='APPROVE_PAYROLL_RUN',
            entity_type='PayrollRun',
            entity_id=str(run.id),
            business=biz,
            reason=f'Approved payroll run for period {run.period_start} to {run.period_end}'
        )

        return Response(PayrollRunSerializer(run).data)


class PayrollRunFinalizeView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        ctx = get_user_context(request)
        biz = ctx['business']
        run = PayrollRun.objects.filter(id=pk, business=biz).first()
        if not run:
            raise NotFound('Payroll run not found.')

        if not PermissionService.has_permission(request.user, 'payroll.finalize', business=biz):
            raise PermissionDenied('You do not have permission to finalize payroll runs.')

        # Enforce approval requirement if enabled in the effective payroll schedule
        from apps.payroll.services.payroll_schedule_service import PayrollScheduleService
        schedule = PayrollScheduleService.resolve_schedule(centre=run.centre, business=biz)
        if schedule['effective_config'].get('approval_required', True):
            if run.status != PayrollRunStatus.APPROVED and not ctx['is_superadmin']:
                raise ValidationError({'detail': 'This payroll run requires approval before it can be finalized.'})

        run = PayrollCalculationService.finalize_payroll_run(run, user=request.user)
        return Response(PayrollRunSerializer(run).data)


class PayrollRunReleaseView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        ctx = get_user_context(request)
        biz = ctx['business']
        run = PayrollRun.objects.filter(id=pk, business=biz).first()
        if not run:
            raise NotFound('Payroll run not found.')

        if not PermissionService.has_permission(request.user, 'payroll.finalize', business=biz) and not ctx['is_superadmin']:
            raise PermissionDenied('You do not have permission to release payroll runs.')

        run = PayrollCalculationService.release_payroll_run(run, user=request.user)
        return Response(PayrollRunSerializer(run).data)


class PayrollRunAdjustView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        ctx = get_user_context(request)
        biz = ctx['business']
        run = PayrollRun.objects.filter(id=pk, business=biz).first()
        if not run:
            raise NotFound('Payroll run not found.')

        if not PermissionService.has_permission(request.user, 'payroll.calculate', business=biz) and not ctx['is_superadmin']:
            raise PermissionDenied('You do not have permission to make adjustments to payroll runs.')

        emp_id = request.data.get('employee_id')
        payroll_id = request.data.get('payroll_id')
        if not emp_id and not payroll_id:
            raise ValidationError({'detail': 'employee_id or payroll_id is required.'})

        if payroll_id:
            payroll = run.payrolls.filter(id=payroll_id).first()
        else:
            payroll = run.payrolls.filter(employee_id=emp_id).first()

        if not payroll:
            raise NotFound('Employee payroll record not found in this run.')

        adj_type = request.data.get('adjustment_type', 'EARNING')
        name = request.data.get('name')
        amount = request.data.get('amount')
        reason = request.data.get('reason')
        is_deduction = bool(request.data.get('is_deduction', False))

        if not name or not str(name).strip():
            raise ValidationError({'name': 'Adjustment item name is required.'})
        if amount is None:
            raise ValidationError({'amount': 'Adjustment amount is required.'})

        adj = PayrollCalculationService.add_or_update_adjustment(
            payroll=payroll,
            adjustment_type=adj_type,
            name=str(name).strip(),
            amount=amount,
            reason=reason,
            user=request.user,
            is_deduction=is_deduction
        )

        from apps.payroll.serializers import PayrollSerializer, PayrollAdjustmentSerializer
        run.refresh_from_db()
        payroll.refresh_from_db()

        return Response({
            'detail': 'Adjustment applied successfully.',
            'adjustment': PayrollAdjustmentSerializer(adj).data,
            'payroll': PayrollSerializer(payroll).data,
            'payroll_run': PayrollRunSerializer(run).data,
        })


class PayrollRecordAdjustView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        ctx = get_user_context(request)
        biz = ctx['business']
        payroll = Payroll.objects.filter(id=pk, business=biz).select_related('payroll_run', 'employee').first()
        if not payroll:
            raise NotFound('Payroll record not found.')

        if not PermissionService.has_permission(request.user, 'payroll.calculate', business=biz) and not ctx['is_superadmin']:
            raise PermissionDenied('You do not have permission to make adjustments to payroll records.')

        adj_type = request.data.get('adjustment_type', 'EARNING')
        name = request.data.get('name')
        amount = request.data.get('amount')
        reason = request.data.get('reason')
        is_deduction = bool(request.data.get('is_deduction', False))

        if not name or not str(name).strip():
            raise ValidationError({'name': 'Adjustment item name is required.'})
        if amount is None:
            raise ValidationError({'amount': 'Adjustment amount is required.'})

        adj = PayrollCalculationService.add_or_update_adjustment(
            payroll=payroll,
            adjustment_type=adj_type,
            name=str(name).strip(),
            amount=amount,
            reason=reason,
            user=request.user,
            is_deduction=is_deduction
        )

        from apps.payroll.serializers import PayrollSerializer, PayrollAdjustmentSerializer
        payroll.refresh_from_db()

        return Response({
            'detail': 'Adjustment applied successfully.',
            'adjustment': PayrollAdjustmentSerializer(adj).data,
            'payroll': PayrollSerializer(payroll).data,
        })


class PayrollRunExceptionsView(views.APIView):
    """
    Lists policy-driven attendance and salary exceptions for a given payroll run.
    Supports filtering by review_status, exception_type, or employee_id.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        ctx = get_user_context(request)
        biz = ctx['business']
        run = PayrollRun.objects.filter(id=pk, business=biz).first()
        if not run:
            raise NotFound('Payroll run not found.')

        if not PermissionService.has_permission(request.user, 'salary.view', business=biz) and not ctx['is_superadmin']:
            raise PermissionDenied('You do not have permission to view payroll exceptions.')

        qs = run.exceptions.select_related('employee', 'payroll', 'decision_by')
        status_param = request.query_params.get('status')
        if status_param and status_param not in ['all', 'ALL', '']:
            qs = qs.filter(review_status=status_param)

        exc_type = request.query_params.get('type')
        if exc_type and exc_type not in ['all', 'ALL', '']:
            qs = qs.filter(exception_type=exc_type)

        emp_id = request.query_params.get('employee_id')
        if emp_id:
            qs = qs.filter(employee_id=emp_id)

        return Response(PayrollExceptionSerializer(qs, many=True).data)


class PayrollExceptionReviewView(views.APIView):
    """
    Review, approve, waive, reject, or adjust amount on an attendance-to-payroll exception
    during the open editing window before finalization.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        exc = PayrollException.objects.filter(id=pk).select_related('payroll_run', 'payroll', 'employee').first()
        if not exc:
            raise NotFound('Payroll exception not found.')

        ctx = get_user_context(request)
        biz = ctx['business']
        if exc.business != biz and not ctx['is_superadmin']:
            raise PermissionDenied('Cross-business access denied.')

        if not PermissionService.has_permission(request.user, 'payroll.calculate', business=exc.business) and not ctx['is_superadmin']:
            raise PermissionDenied('You do not have permission to review payroll exceptions.')

        action = request.data.get('action')  # APPROVE, WAIVE, REJECT, ADJUST_AMOUNT
        if not action or action not in ['APPROVE', 'WAIVE', 'REJECT', 'ADJUST_AMOUNT']:
            raise ValidationError({'action': 'Action must be one of: APPROVE, WAIVE, REJECT, ADJUST_AMOUNT.'})

        reason = request.data.get('reason') or request.data.get('decision_reason', '')
        if not str(reason).strip():
            raise ValidationError({'reason': 'A mandatory audit reason is required for reviewing payroll exceptions.'})

        amount = request.data.get('amount') or request.data.get('decision_amount')
        if action == 'ADJUST_AMOUNT':
            if amount is None:
                raise ValidationError({'amount': 'decision_amount is required when action is ADJUST_AMOUNT.'})
            from decimal import Decimal
            try:
                amount = Decimal(str(amount))
                if amount < 0:
                    raise ValidationError({'amount': 'decision_amount cannot be negative.'})
            except Exception:
                raise ValidationError({'amount': 'Invalid decision_amount.'})

        updated_exc = PayrollCalculationService.review_payroll_exception(
            exception=exc,
            action=action,
            amount=amount,
            reason=str(reason).strip(),
            user=request.user
        )

        payroll = updated_exc.payroll
        payroll.refresh_from_db()
        return Response({
            'detail': f'Exception successfully updated with action {action}.',
            'exception': PayrollExceptionSerializer(updated_exc).data,
            'payroll': PayrollSerializer(payroll).data,
        })
