from rest_framework import serializers
from apps.payroll.models import Payroll, Payslip, SalaryStructure


class PayslipSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payslip
        fields = ['id', 'storage_path', 'file_name', 'generated_at']


class PayrollSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    employee_id_code = serializers.CharField(source='employee.employee_id', read_only=True)
    department_name = serializers.CharField(source='employee.department.name', read_only=True, default='')
    payslip = PayslipSerializer(read_only=True)

    class Meta:
        model = Payroll
        fields = [
            'id', 'employee', 'employee_name', 'employee_id_code', 'department_name',
            'period_start', 'period_end', 'gross_amount', 'total_deductions',
            'net_amount', 'currency', 'status', 'generated_at', 'payslip'
        ]


class SalaryStructureSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)

    class Meta:
        model = SalaryStructure
        fields = ['id', 'employee', 'employee_name', 'effective_from', 'effective_to', 'basic_salary', 'currency']


class SalaryRevisionSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    revised_by_name = serializers.CharField(source='revised_by.get_full_name', read_only=True)

    class Meta:
        from apps.payroll.models import SalaryRevision
        model = SalaryRevision
        fields = [
            'id', 'business', 'employee', 'employee_name', 'effective_from', 'effective_to',
            'basic_salary', 'hourly_rate', 'ot_rate', 'currency', 'allowances',
            'deduction_rules', 'reason', 'revised_by', 'revised_by_name', 'created_at'
        ]
        read_only_fields = ['id', 'business', 'employee', 'revised_by', 'created_at']


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

    class Meta:
        from apps.payroll.models import EmployeeCompensationItem
        model = EmployeeCompensationItem
        fields = [
            'id', 'business', 'employee', 'employee_name', 'name', 'component_type',
            'calculation_type', 'amount', 'effective_from', 'effective_to',
            'reason', 'notes', 'is_active', 'created_by', 'created_by_name',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'business', 'employee', 'created_by', 'created_at', 'updated_at']
