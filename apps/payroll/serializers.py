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
