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


class PayrollSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    employee_id_code = serializers.CharField(source='employee.employee_id', read_only=True)
    department_name = serializers.CharField(source='employee.department.name', read_only=True, default='')
    payslip = PayslipSerializer(read_only=True)
    line_items = PayrollLineItemSerializer(many=True, read_only=True)

    class Meta:
        model = Payroll
        fields = [
            'id', 'employee', 'employee_name', 'employee_id_code', 'department_name',
            'period_start', 'period_end', 'paid_days', 'unpaid_days', 'half_days',
            'ot_hours', 'gross_amount', 'total_deductions', 'net_amount',
            'currency', 'status', 'generated_at', 'payslip', 'line_items'
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

    class Meta:
        from apps.payroll.models import SalaryRevision
        model = SalaryRevision
        fields = [
            'id', 'business', 'employee', 'employee_name', 'effective_from', 'effective_to',
            'basic_salary', 'hourly_rate', 'ot_rate', 'currency', 'allowances',
            'deduction_rules', 'reason', 'revised_by', 'revised_by_name',
            'is_upcoming', 'is_current', 'status', 'created_at'
        ]
        read_only_fields = ['id', 'business', 'employee', 'revised_by', 'created_at']

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

    class Meta:
        from apps.payroll.models import PayrollRun
        model = PayrollRun
        fields = [
            'id', 'business', 'centre', 'centre_name', 'period_start', 'period_end',
            'status', 'total_employees', 'total_gross', 'total_deductions', 'total_net',
            'approved_by', 'approved_by_name', 'finalized_at', 'created_at'
        ]
        read_only_fields = ['id', 'business', 'approved_by', 'finalized_at', 'created_at']


class EmployeeCompensationItemSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    is_upcoming = serializers.SerializerMethodField()
    is_effective_now = serializers.SerializerMethodField()
    status_display = serializers.SerializerMethodField()

    class Meta:
        from apps.payroll.models import EmployeeCompensationItem
        model = EmployeeCompensationItem
        fields = [
            'id', 'business', 'employee', 'employee_name', 'name', 'component_type',
            'calculation_type', 'frequency', 'amount', 'effective_from', 'effective_to',
            'reason', 'notes', 'is_active', 'is_upcoming', 'is_effective_now', 'status_display',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'business', 'employee', 'created_by', 'created_at', 'updated_at']

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
        if not obj.is_active:
            return 'DEACTIVATED'
        if self.get_is_upcoming(obj):
            return 'UPCOMING'
        if obj.effective_to:
            from django.utils import timezone
            today = timezone.localdate() if hasattr(timezone, 'localdate') else timezone.now().date()
            if obj.effective_to < today:
                return 'EXPIRED'
        return 'ACTIVE'

