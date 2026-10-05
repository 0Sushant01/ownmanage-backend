from rest_framework import serializers
from apps.attendance.models import AttendanceDay, AttendanceEvent


class AttendanceEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttendanceEvent
        fields = [
            'id', 'event_type', 'event_time', 'latitude', 'longitude',
            'location_accuracy', 'device_id', 'source', 'notes'
        ]
        read_only_fields = ['id', 'event_time']


class AttendanceDaySerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    employee_id_code = serializers.CharField(source='employee.employee_id', read_only=True)
    events = AttendanceEventSerializer(many=True, read_only=True)
    work_hours_display = serializers.SerializerMethodField()

    class Meta:
        model = AttendanceDay
        fields = [
            'id', 'employee', 'employee_name', 'employee_id_code',
            'attendance_date', 'status', 'total_work_seconds',
            'work_hours_display', 'is_locked', 'notes', 'events', 'created_at'
        ]

    def get_work_hours_display(self, obj):
        total_seconds = obj.total_work_seconds or 0
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        return f"{hours:02d}h {minutes:02d}m"


class WorkScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        from apps.attendance.models import WorkSchedule
        model = WorkSchedule
        fields = [
            'id', 'business', 'name', 'start_time', 'end_time',
            'is_overnight', 'grace_period_minutes', 'is_business_default',
            'is_active', 'created_at'
        ]
        read_only_fields = ['id', 'business', 'created_at']


class AttendanceCorrectionSerializer(serializers.ModelSerializer):
    requested_by_name = serializers.CharField(source='requested_by.full_name', read_only=True)
    reviewed_by_name = serializers.CharField(source='reviewed_by.full_name', read_only=True)
    employee_name = serializers.CharField(source='attendance_day.employee.full_name', read_only=True)
    attendance_date = serializers.CharField(source='attendance_day.attendance_date', read_only=True)

    class Meta:
        from apps.attendance.models import AttendanceCorrection
        model = AttendanceCorrection
        fields = [
            'id', 'business', 'attendance_day', 'attendance_date', 'employee_name',
            'original_event', 'requested_by', 'requested_by_name',
            'reviewed_by', 'reviewed_by_name', 'correction_type',
            'corrected_time', 'corrected_status', 'reason', 'status',
            'reviewed_at', 'review_notes', 'created_at'
        ]
        read_only_fields = ['id', 'business', 'requested_by', 'reviewed_by', 'reviewed_at', 'created_at']


class AttendancePolicySerializer(serializers.ModelSerializer):
    class Meta:
        from apps.attendance.models import AttendancePolicy
        model = AttendancePolicy
        fields = [
            'id', 'business', 'office_start', 'office_end', 'break_start', 'break_end',
            'working_days', 'weekly_off', 'weekly_off_days', 'daily_schedules',
            'grace_period_minutes', 'minimum_present_minutes', 'minimum_half_day_minutes',
            'late_threshold_minutes', 'early_checkout_threshold_minutes',
            'auto_attendance', 'allow_center_override', 'ot_enabled', 'ot_grace_minutes',
            'ot_approval_required', 'max_daily_ot_minutes', 'allow_normal_punch',
            'allow_gps', 'allow_geofencing', 'allow_qr', 'allow_face_recognition',
            'allow_biometric', 'gps_latitude', 'gps_longitude', 'gps_radius_meters',
            'location_required_checkin', 'location_required_checkout',
            'extra_settings', 'created_at'
        ]
        read_only_fields = ['id', 'business', 'created_at']


class AttendancePolicyOverrideSerializer(serializers.ModelSerializer):
    class Meta:
        from apps.attendance.models import AttendancePolicyOverride
        model = AttendancePolicyOverride
        fields = [
            'id', 'centre', 'office_start', 'office_end', 'break_start', 'break_end',
            'weekly_off', 'weekly_off_days', 'daily_schedules',
            'grace_period_minutes', 'minimum_present_minutes', 'minimum_half_day_minutes',
            'late_threshold_minutes', 'early_checkout_threshold_minutes',
            'auto_attendance', 'ot_enabled', 'ot_grace_minutes',
            'allow_normal_punch', 'allow_gps', 'allow_geofencing', 'allow_qr',
            'allow_face_recognition', 'allow_biometric',
            'gps_latitude', 'gps_longitude', 'gps_radius_meters',
            'location_required_checkin', 'location_required_checkout',
            'extra_settings', 'created_at'
        ]
        read_only_fields = ['id', 'centre', 'created_at']

