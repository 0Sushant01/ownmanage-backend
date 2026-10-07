from rest_framework import serializers
from apps.biometrics.models import (
    EmployeeFaceEnrollment,
    EmployeeDevice,
    BiometricAuditEvent,
    BiometricChallenge
)


class EmployeeFaceEnrollmentSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    enrolled_by_name = serializers.CharField(source='enrolled_by.get_full_name', read_only=True, default='—')

    class Meta:
        model = EmployeeFaceEnrollment
        fields = [
            'id',
            'employee',
            'employee_name',
            'model_id',
            'model_version',
            'detector_version',
            'preprocessing_version',
            'embedding_dimension',
            'embedding_format',
            'status',
            'quality_score',
            'enrolled_at',
            'enrolled_by',
            'enrolled_by_name',
            'revoked_at',
            'revocation_reason',
            'created_at',
            'updated_at',
        ]
        read_only_fields = fields


class EmployeeDeviceSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)

    class Meta:
        model = EmployeeDevice
        fields = [
            'id',
            'employee',
            'employee_name',
            'device_id',
            'device_name',
            'platform',
            'status',
            'registered_at',
            'last_seen_at',
            'revoked_at',
            'revocation_reason',
        ]
        read_only_fields = ['id', 'status', 'registered_at', 'last_seen_at', 'revoked_at', 'revocation_reason']


class BiometricAuditEventSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    actor_name = serializers.CharField(source='actor.get_full_name', read_only=True, default='System')

    class Meta:
        model = BiometricAuditEvent
        fields = [
            'id',
            'business',
            'employee',
            'employee_name',
            'event_type',
            'actor',
            'actor_name',
            'details',
            'ip_address',
            'created_at',
        ]
        read_only_fields = fields


class DeviceRegistrationInputSerializer(serializers.Serializer):
    device_id = serializers.CharField(max_length=150)
    device_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    platform = serializers.ChoiceField(choices=['ANDROID', 'IOS', 'WEB'], default='ANDROID')
    public_key = serializers.CharField(required=False, allow_blank=True)


class RevokeDeviceInputSerializer(serializers.Serializer):
    device_id = serializers.CharField(max_length=150)
    reason = serializers.CharField(required=False, allow_blank=True)
