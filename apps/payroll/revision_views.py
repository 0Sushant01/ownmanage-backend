from datetime import date
from django.utils import timezone
from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, NotFound, ValidationError

from apps.core.permissions import get_user_context
from apps.organization.models import Business, BusinessRole, Employee, Branch
from apps.payroll.models import SalaryRevision, PayrollRun, PayrollRunStatus, PayrollStatus
from apps.payroll.serializers import SalaryRevisionSerializer, PayrollRunSerializer, PayrollSerializer
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

        revision = serializer.save(
            business=emp.business,
            employee=emp,
            revised_by=request.user
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

        comparisons = []
        for i in range(len(revisions)):
            curr = revisions[i]
            prev = revisions[i + 1] if i + 1 < len(revisions) else None

            curr_val = float(curr.basic_salary)
            prev_val = float(prev.basic_salary) if prev else 0.0
            diff = curr_val - prev_val
            pct = round((diff / prev_val * 100), 2) if prev_val > 0 else 0.0

            is_upcoming = (curr.effective_from > today)
            is_current = (str(curr.id) == current_rev_id)

            comparisons.append({
                'revision_id': str(curr.id),
                'effective_from': str(curr.effective_from),
                'effective_to': str(curr.effective_to) if curr.effective_to else 'Present',
                'new_salary': curr_val,
                'previous_salary': prev_val if prev else None,
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

    def get(self, request, pk):
        emp = self.get_employee(request, pk)
        from apps.payroll.models import EmployeeCompensationItem
        from apps.payroll.serializers import EmployeeCompensationItemSerializer

        qs = EmployeeCompensationItem.objects.filter(employee=emp).select_related('created_by')
        component_type = request.query_params.get('type')
        if component_type:
            qs = qs.filter(component_type=component_type.upper())

        active_only = request.query_params.get('active_only')
        if active_only and active_only.lower() in ['true', '1']:
            qs = qs.filter(is_active=True)

        return Response(EmployeeCompensationItemSerializer(qs, many=True).data)

    def post(self, request, pk):
        emp = self.get_employee(request, pk)
        if not PermissionService.has_permission(request.user, 'salary.edit', business=emp.business, target_employee=emp):
            raise PermissionDenied('You do not have permission to manage employee compensation items.')

        from apps.payroll.serializers import EmployeeCompensationItemSerializer
        from apps.organization.models import EmployeeActivityLog

        serializer = EmployeeCompensationItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = serializer.save(
            business=emp.business,
            employee=emp,
            created_by=request.user
        )

        EmployeeActivityLog.objects.create(
            business=emp.business,
            employee=emp,
            activity_type='COMPENSATION_ITEM_ADDED',
            description=f"Added {item.component_type}: {item.name} ({item.amount})",
            new_value=serializer.data,
            performed_by=request.user
        )

        AuditService.log(
            user_or_request=request,
            action='ADD_COMPENSATION_ITEM',
            entity_type='EmployeeCompensationItem',
            entity_id=str(item.id),
            new_data=serializer.data,
            business=emp.business,
            reason=f'Added {item.component_type} component {item.name} for {emp.full_name}'
        )

        return Response(EmployeeCompensationItemSerializer(item).data, status=status.HTTP_201_CREATED)


class EmployeeCompensationItemDetailView(views.APIView):
    """
    Manage single compensation item (Update or Soft-Deactivate / End-Date).
    """
    permission_classes = [IsAuthenticated]

    def get_item(self, request, pk, comp_pk):
        from apps.payroll.models import EmployeeCompensationItem
        item = EmployeeCompensationItem.objects.filter(id=comp_pk, employee_id=pk).select_related('business', 'employee').first()
        if not item:
            raise NotFound('Compensation component not found.')

        ctx = get_user_context(request)
        if not ctx['is_superadmin'] and item.business_id != ctx['business'].id:
            raise PermissionDenied('Cross-tenant access forbidden.')

        return item

    def patch(self, request, pk, comp_pk):
        item = self.get_item(request, pk, comp_pk)
        if not PermissionService.has_permission(request.user, 'salary.edit', business=item.business, target_employee=item.employee):
            raise PermissionDenied('Permission denied to update compensation item.')

        from apps.payroll.serializers import EmployeeCompensationItemSerializer
        old_data = EmployeeCompensationItemSerializer(item).data

        serializer = EmployeeCompensationItemSerializer(item, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated = serializer.save()

        AuditService.log(
            user_or_request=request,
            action='UPDATE_COMPENSATION_ITEM',
            entity_type='EmployeeCompensationItem',
            entity_id=str(updated.id),
            old_data=old_data,
            new_data=serializer.data,
            business=item.business,
            reason=f'Updated compensation item {item.name} for {item.employee.full_name}'
        )

        return Response(EmployeeCompensationItemSerializer(updated).data)

    def delete(self, request, pk, comp_pk):
        """
        Soft deactivates the compensation component without destroying historical records.
        """
        item = self.get_item(request, pk, comp_pk)
        if not PermissionService.has_permission(request.user, 'salary.edit', business=item.business, target_employee=item.employee):
            raise PermissionDenied('Permission denied to deactivate compensation item.')

        from apps.payroll.serializers import EmployeeCompensationItemSerializer
        from apps.organization.models import EmployeeActivityLog

        old_data = EmployeeCompensationItemSerializer(item).data
        item.is_active = False
        item.effective_to = date.today()
        item.save(update_fields=['is_active', 'effective_to', 'updated_at'])

        EmployeeActivityLog.objects.create(
            business=item.business,
            employee=item.employee,
            activity_type='COMPENSATION_ITEM_DEACTIVATED',
            description=f"Deactivated {item.component_type}: {item.name}",
            old_value=old_data,
            new_value={'is_active': False, 'effective_to': str(item.effective_to)},
            performed_by=request.user
        )

        AuditService.log(
            user_or_request=request,
            action='DEACTIVATE_COMPENSATION_ITEM',
            entity_type='EmployeeCompensationItem',
            entity_id=str(item.id),
            business=item.business,
            reason=f'Deactivated compensation component {item.name} for {item.employee.full_name}'
        )

        return Response({
            'detail': f"Compensation component '{item.name}' deactivated successfully.",
            'item': EmployeeCompensationItemSerializer(item).data
        })


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

        return Response(PayrollRunSerializer(qs.select_related('centre', 'approved_by')[:50], many=True).data)

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

        run.status = PayrollRunStatus.FINALIZED
        run.finalized_at = timezone.now()
        run.save(update_fields=['status', 'finalized_at', 'updated_at'])

        # Mark all itemized payroll records as PAID / FINAL
        run.payrolls.update(status=PayrollStatus.PAID)

        AuditService.log(
            user_or_request=request,
            action='FINALIZE_PAYROLL_RUN',
            entity_type='PayrollRun',
            entity_id=str(run.id),
            business=biz,
            reason=f'Finalized and locked payroll run for period {run.period_start} to {run.period_end}'
        )

        return Response(PayrollRunSerializer(run).data)
