from rest_framework import serializers
from apps.payroll.models import Payroll, Payslip, SalaryStructure


class PayslipSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payslip
        fields = ['id', 'storage_path', 'file_name', 'generated_at']


class PayrollLineItemSerializer(serializers.ModelSerializer):
    class Meta:
        from apps.payroll.models import PayrollLineItem
        model = PayrollLineItem
        fields = [
            'id', 'payroll', 'name', 'line_type', 'amount', 'rate', 'units',
            'is_deduction', 'source_compensation_item', 'created_at'
        ]
        read_only_fields = ['id', 'payroll', 'created_at']


class PayrollExceptionSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    employee_id_code = serializers.CharField(source='employee.employee_id', read_only=True)
    centre_name = serializers.CharField(source='centre.name', read_only=True, default='')
    decision_by_name = serializers.CharField(source='decision_by.get_full_name', read_only=True, default='')

    class Meta:
        from apps.payroll.models import PayrollException
        model = PayrollException
        fields = [
            'id', 'business', 'payroll_run', 'payroll', 'employee', 'employee_name',
            'employee_id_code', 'centre', 'centre_name', 'attendance_date',
            'attendance_status', 'scheduled_hours', 'actual_hours', 'exception_type',
            'exception_reason', 'proposed_amount', 'is_deduction', 'policy_applied',
            'review_status', 'decision_amount', 'decision_by', 'decision_by_name',
            'decision_at', 'decision_reason', 'is_applied_to_payroll',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'business', 'payroll_run', 'payroll', 'employee', 'centre',
            'attendance_date', 'attendance_status', 'scheduled_hours', 'actual_hours',
            'exception_type', 'exception_reason', 'proposed_amount', 'is_deduction',
            'policy_applied', 'created_at', 'updated_at'
        ]


class PayrollSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    employee_id_code = serializers.CharField(source='employee.employee_id', read_only=True)
    department_name = serializers.CharField(source='employee.department.name', read_only=True, default='')
    payslip = PayslipSerializer(read_only=True)
    line_items = PayrollLineItemSerializer(many=True, read_only=True)
    exceptions = PayrollExceptionSerializer(many=True, read_only=True)

    class Meta:
        model = Payroll
        fields = [
            'id', 'employee', 'employee_name', 'employee_id_code', 'department_name',
            'period_start', 'period_end', 'paid_days', 'unpaid_days', 'half_days',
            'ot_hours', 'gross_amount', 'total_deductions', 'net_amount',
            'currency', 'status', 'editing_deadline', 'finalized_at', 'released_at',
            'visibility_policy', 'compensation_type', 'salary_unit', 'base_salary_amount',
            'is_visible_to_employee', 'generated_at', 'payslip', 'line_items',
            'exceptions', 'salary_snapshot', 'schedule_snapshot'
        ]


class SalaryStructureSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)

    class Meta:
        model = SalaryStructure
        fields = ['id', 'employee', 'employee_name', 'effective_from', 'effective_to', 'basic_salary', 'currency']


class SalaryRevisionSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    revised_by_name = serializers.CharField(source='revised_by.get_full_name', read_only=True)
    is_upcoming = serializers.SerializerMethodField()
    is_current = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    salary_unit_display = serializers.SerializerMethodField()

    class Meta:
        from apps.payroll.models import SalaryRevision
        model = SalaryRevision
        fields = [
            'id', 'business', 'employee', 'employee_name', 'effective_from', 'effective_to',
            'basic_salary', 'salary_unit', 'salary_unit_display', 'hourly_rate', 'ot_rate', 'currency', 'allowances',
            'deduction_rules', 'reason', 'revised_by', 'revised_by_name',
            'is_upcoming', 'is_current', 'status', 'created_at'
        ]
        read_only_fields = ['id', 'business', 'employee', 'revised_by', 'created_at']

    def get_salary_unit_display(self, obj) -> str:
        u = getattr(obj, 'salary_unit', 'MONTHLY') or 'MONTHLY'
        if u == 'DAILY':
            return '/ day'
        elif u == 'WEEKLY':
            return '/ week'
        return '/ month'

    def get_is_upcoming(self, obj) -> bool:
        from django.utils import timezone
        today = timezone.localdate() if hasattr(timezone, 'localdate') else timezone.now().date()
        return obj.effective_from > today

    def get_is_current(self, obj) -> bool:
        from django.utils import timezone
        today = timezone.localdate() if hasattr(timezone, 'localdate') else timezone.now().date()
        if obj.effective_from > today:
            return False
        if obj.effective_to and obj.effective_to < today:
            return False
        from apps.payroll.models import SalaryRevision
        newer = SalaryRevision.objects.filter(
            employee_id=obj.employee_id,
            effective_from__lte=today,
            effective_from__gt=obj.effective_from
        ).exists()
        return not newer

    def get_status(self, obj) -> str:
        if self.get_is_upcoming(obj):
            return 'UPCOMING'
        if self.get_is_current(obj):
            return 'CURRENT'
        return 'HISTORICAL'


class PayrollRunSerializer(serializers.ModelSerializer):
    centre_name = serializers.CharField(source='centre.name', read_only=True)
    approved_by_name = serializers.CharField(source='approved_by.get_full_name', read_only=True)
    is_editing_open = serializers.BooleanField(read_only=True)

    class Meta:
        from apps.payroll.models import PayrollRun
        model = PayrollRun
        fields = [
            'id', 'business', 'centre', 'centre_name', 'period_start', 'period_end',
            'status', 'total_employees', 'total_gross', 'total_deductions', 'total_net',
            'approved_by', 'approved_by_name', 'finalized_at', 'released_at', 'created_at',
            'editing_deadline', 'is_editing_open', 'visibility_policy', 'generation_type',
            'expected_payment_date', 'generation_mode', 'pay_frequency', 'schedule_config',
            'schedule_config_snapshot'
        ]
        read_only_fields = ['id', 'business', 'approved_by', 'finalized_at', 'released_at', 'created_at']


class PayrollAdjustmentSerializer(serializers.ModelSerializer):
    adjusted_by_name = serializers.CharField(source='adjusted_by.get_full_name', read_only=True)
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)

    class Meta:
        from apps.payroll.models import PayrollAdjustment
        model = PayrollAdjustment
        fields = [
            'id', 'business', 'payroll_run', 'payroll', 'employee', 'employee_name',
            'adjustment_type', 'name', 'previous_amount', 'new_amount', 'is_deduction',
            'reason', 'adjusted_by', 'adjusted_by_name', 'created_at'
        ]
        read_only_fields = ['id', 'business', 'created_at']


class EmployeeCompensationItemSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    centre_name = serializers.CharField(source='centre.name', read_only=True)
    is_upcoming = serializers.SerializerMethodField()
    is_effective_now = serializers.SerializerMethodField()
    status_display = serializers.SerializerMethodField()
    recurrence_type = serializers.CharField(read_only=True)
    recurrence_frequency = serializers.CharField(read_only=True)
    source = serializers.SerializerMethodField()
    source_display = serializers.SerializerMethodField()

    class Meta:
        from apps.payroll.models import EmployeeCompensationItem
        model = EmployeeCompensationItem
        fields = [
            'id', 'business', 'scope', 'centre', 'centre_name', 'employee', 'employee_name',
            'parent_item', 'is_override', 'name', 'component_type', 'calculation_type',
            'frequency', 'recurrence_type', 'recurrence_frequency', 'amount',
            'effective_from', 'effective_to', 'affects_payroll', 'is_applied',
            'applied_in_payroll', 'applied_at', 'reason', 'notes', 'is_active',
            'is_upcoming', 'is_effective_now', 'status_display', 'source', 'source_display',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'business', 'created_by', 'created_at', 'updated_at', 'is_applied', 'applied_in_payroll', 'applied_at']

    def validate(self, attrs):
        amount = attrs.get('amount')
        calc_type = attrs.get('calculation_type')
        if amount is not None:
            if amount < 0:
                raise serializers.ValidationError({'amount': 'Amount cannot be negative.'})
            if calc_type == 'PERCENTAGE' and amount > 100:
                raise serializers.ValidationError({'amount': 'Percentage cannot exceed 100%.'})

        eff_from = attrs.get('effective_from') or (self.instance.effective_from if self.instance else None)
        eff_to = attrs.get('effective_to') or (self.instance.effective_to if self.instance else None)
        if eff_from and eff_to and eff_to < eff_from:
            raise serializers.ValidationError({'effective_to': 'Effective end date cannot precede effective start date.'})

        return attrs

    def get_is_upcoming(self, obj) -> bool:
        if not obj.effective_from:
            return False
        from django.utils import timezone
        today = timezone.localdate() if hasattr(timezone, 'localdate') else timezone.now().date()
        return obj.effective_from > today

    def get_is_effective_now(self, obj) -> bool:
        if not obj.is_active:
            return False
        from django.utils import timezone
        today = timezone.localdate() if hasattr(timezone, 'localdate') else timezone.now().date()
        if obj.effective_from and obj.effective_from > today:
            return False
        if obj.effective_to and obj.effective_to < today:
            return False
        return True

    def get_status_display(self, obj) -> str:
        from apps.payroll.services.compensation_resolver import CompensationResolver
        from django.utils import timezone
        today = timezone.localdate() if hasattr(timezone, 'localdate') else timezone.now().date()
        return CompensationResolver.compute_status(obj, today)

    def get_source(self, obj) -> str:
        return getattr(obj, '_source', obj.scope)

    def get_source_display(self, obj) -> str:
        if hasattr(obj, '_source_display'):
            return obj._source_display
        scope_map = {
            'ENTERPRISE': 'Enterprise Default',
            'CENTRE': 'Centre Override',
            'EMPLOYEE': 'Employee Override' if obj.is_override else 'Employee Specific',
        }
        return scope_map.get(obj.scope, 'Employee Specific')


class PayrollScheduleConfigSerializer(serializers.ModelSerializer):
    changed_by_name = serializers.CharField(source='changed_by.get_full_name', read_only=True)
    centre_name = serializers.CharField(source='centre.name', read_only=True)
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    generation_date = serializers.IntegerField(source='generation_day_of_month', required=False, allow_null=True)

    class Meta:
        from apps.payroll.models import PayrollScheduleConfig
        model = PayrollScheduleConfig
        fields = [
            'id', 'business', 'centre', 'centre_name', 'employee', 'employee_name',
            'scope', 'has_override', 'is_active', 'effective_from', 'effective_to',
            'generation_type', 'generation_weekday', 'generation_date',
            'compensation_type', 'pay_frequency', 'week_start_day', 'custom_cycle_start_day',
            'anchor_date', 'month_end_rule', 'generation_mode', 'generation_delay_days',
            'generation_day_of_month', 'approval_required', 'approver_role',
            'review_deadline_days', 'editable_period_duration', 'editing_period_unit',
            'finalization_deadline_days', 'visibility_policy',
            'payment_rule', 'payment_offset_days',
            'payment_day_of_month', 'payment_weekday', 'change_reason',
            'changed_by', 'changed_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'business', 'created_at', 'updated_at']


class PayrollScheduleHistorySerializer(serializers.ModelSerializer):
    changed_by_name = serializers.CharField(source='changed_by.get_full_name', read_only=True)

    class Meta:
        from apps.payroll.models import PayrollScheduleHistory
        model = PayrollScheduleHistory
        fields = [
            'id', 'config', 'business', 'centre', 'employee', 'scope', 'snapshot',
            'effective_from', 'effective_to', 'change_reason', 'changed_by',
            'changed_by_name', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']

