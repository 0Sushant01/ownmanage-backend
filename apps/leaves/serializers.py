from rest_framework import serializers
from apps.leaves.models import LeaveType, LeaveRequest, LeaveRequestStatus


class LeaveTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveType
        fields = ['id', 'name', 'code', 'description', 'is_paid', 'is_active']
        read_only_fields = ['id']


class LeaveRequestSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    employee_id_code = serializers.CharField(source='employee.employee_id', read_only=True)
    leave_type_name = serializers.CharField(source='leave_type.name', read_only=True)
    leave_type_code = serializers.CharField(source='leave_type.code', read_only=True)
    approved_by_name = serializers.CharField(source='approved_by.full_name', read_only=True, default='')

    class Meta:
        model = LeaveRequest
        fields = [
            'id', 'employee', 'employee_name', 'employee_id_code',
            'leave_type', 'leave_type_name', 'leave_type_code',
            'start_date', 'end_date', 'reason', 'status',
            'approved_by', 'approved_by_name', 'approved_at',
            'rejected_at', 'rejection_reason', 'created_at'
        ]
        read_only_fields = ['id', 'status', 'approved_by', 'approved_at', 'rejected_at', 'created_at']


class LeaveRequestCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveRequest
        fields = ['leave_type', 'start_date', 'end_date', 'reason']

    def validate(self, attrs):
        start_date = attrs.get('start_date')
        end_date = attrs.get('end_date')
        if start_date and end_date and end_date < start_date:
            raise serializers.ValidationError({'end_date': 'End date must be on or after start date.'})
        return attrs

    def create(self, validated_data):
        employee = self.context['employee']
        return LeaveRequest.objects.create(
            business=employee.business,
            employee=employee,
            status=LeaveRequestStatus.PENDING,
            **validated_data
        )
