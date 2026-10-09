from rest_framework import serializers
from apps.meetings.models import (
    Meeting, MeetingParticipant, MeetingExternalGuest, MeetingAuditEvent,
    MeetingStatus, MeetingLocationType, ParticipantResponseStatus
)
from apps.organization.models import Employee


class MeetingParticipantSerializer(serializers.ModelSerializer):
    employee_id_code = serializers.CharField(source='employee.employee_id', read_only=True)
    full_name = serializers.CharField(source='employee.full_name', read_only=True)
    email = serializers.CharField(source='employee.email', read_only=True)
    designation = serializers.CharField(source='employee.designation', read_only=True)
    branch_name = serializers.SerializerMethodField()

    class Meta:
        model = MeetingParticipant
        fields = [
            'id', 'employee', 'employee_id_code', 'full_name', 'email',
            'designation', 'branch_name', 'response_status', 'response_note',
            'responded_at', 'is_organizer', 'is_optional'
        ]

    def get_branch_name(self, obj):
        return obj.employee.branch.name if obj.employee.branch else 'Unassigned'


class MeetingExternalGuestSerializer(serializers.ModelSerializer):
    class Meta:
        model = MeetingExternalGuest
        fields = ['id', 'email', 'name', 'response_status', 'responded_at']


class MeetingAuditEventSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = MeetingAuditEvent
        fields = ['id', 'event_type', 'summary', 'diff_data', 'actor_name', 'created_at']

    def get_actor_name(self, obj):
        return obj.actor.get_full_name() if obj.actor else 'System'


class MeetingListSerializer(serializers.ModelSerializer):
    branch_name = serializers.SerializerMethodField()
    organizer_name = serializers.SerializerMethodField()
    participant_count = serializers.SerializerMethodField()
    my_response_status = serializers.SerializerMethodField()

    class Meta:
        model = Meeting
        fields = [
            'id', 'title', 'meeting_date', 'start_time', 'end_time', 'timezone',
            'location_type', 'location_details', 'meeting_url', 'status',
            'branch', 'branch_name', 'organizer', 'organizer_name',
            'participant_count', 'my_response_status', 'created_at'
        ]

    def get_branch_name(self, obj):
        return obj.branch.name if obj.branch else None

    def get_organizer_name(self, obj):
        if obj.organizer_employee:
            return obj.organizer_employee.full_name
        return obj.organizer.get_full_name() if obj.organizer else 'Unknown'

    def get_participant_count(self, obj):
        internal_count = obj.participants.count()
        external_count = obj.external_guests.count()
        return {
            'total': internal_count + external_count,
            'internal': internal_count,
            'external': external_count,
            'accepted': obj.participants.filter(response_status='ACCEPTED').count(),
            'declined': obj.participants.filter(response_status='DECLINED').count(),
            'pending': obj.participants.filter(response_status='PENDING').count(),
        }

    def get_my_response_status(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return None
        part = obj.participants.filter(employee__user=request.user).first()
        return part.response_status if part else None


class MeetingDetailSerializer(serializers.ModelSerializer):
    branch_name = serializers.SerializerMethodField()
    organizer_name = serializers.SerializerMethodField()
    participants = MeetingParticipantSerializer(many=True, read_only=True)
    external_guests = MeetingExternalGuestSerializer(many=True, read_only=True)
    audit_events = MeetingAuditEventSerializer(many=True, read_only=True)
    my_response_status = serializers.SerializerMethodField()
    can_edit = serializers.SerializerMethodField()
    can_cancel = serializers.SerializerMethodField()

    class Meta:
        model = Meeting
        fields = [
            'id', 'title', 'description', 'meeting_date', 'start_time', 'end_time',
            'timezone', 'location_type', 'location_details', 'meeting_url', 'status',
            'cancellation_reason', 'cancelled_at', 'branch', 'branch_name',
            'organizer', 'organizer_name', 'participants', 'external_guests',
            'audit_events', 'my_response_status', 'can_edit', 'can_cancel',
            'created_at', 'updated_at'
        ]

    def get_branch_name(self, obj):
        return obj.branch.name if obj.branch else None

    def get_organizer_name(self, obj):
        if obj.organizer_employee:
            return obj.organizer_employee.full_name
        return obj.organizer.get_full_name() if obj.organizer else 'Unknown'

    def get_my_response_status(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return None
        part = obj.participants.filter(employee__user=request.user).first()
        return part.response_status if part else None

    def get_can_edit(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        if request.user.is_superuser or obj.organizer_id == request.user.id:
            return True
        from apps.organization.services.permission_service import PermissionService
        return PermissionService.has_permission(request.user, 'meetings.edit_any', obj.business)

    def get_can_cancel(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        if request.user.is_superuser or obj.organizer_id == request.user.id:
            return True
        from apps.organization.services.permission_service import PermissionService
        return PermissionService.has_permission(request.user, 'meetings.cancel_any', obj.business)


class MeetingCreateUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, allow_blank=True, default='')
    meeting_date = serializers.DateField()
    start_time = serializers.TimeField()
    end_time = serializers.TimeField()
    timezone = serializers.CharField(max_length=50, default='Asia/Kolkata')
    location_type = serializers.ChoiceField(choices=MeetingLocationType.choices, default=MeetingLocationType.ONLINE)
    location_details = serializers.CharField(required=False, allow_blank=True, default='')
    meeting_url = serializers.URLField(required=False, allow_null=True, allow_blank=True, default=None)
    branch_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    participant_ids = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    external_guests = serializers.ListField(child=serializers.DictField(), required=False, default=list)


class MeetingConflictCheckSerializer(serializers.Serializer):
    meeting_date = serializers.DateField()
    start_time = serializers.TimeField()
    end_time = serializers.TimeField()
    participant_ids = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    exclude_meeting_id = serializers.UUIDField(required=False, allow_null=True, default=None)
