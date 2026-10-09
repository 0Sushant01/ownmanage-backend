from django.contrib import admin
from .models import Meeting, MeetingParticipant, MeetingExternalGuest, MeetingAuditEvent, MeetingNotificationOutbox


@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    list_display = ('title', 'business', 'branch', 'meeting_date', 'start_time', 'end_time', 'organizer', 'status')
    list_filter = ('status', 'location_type', 'meeting_date', 'business')
    search_fields = ('title', 'description', 'organizer__email')


@admin.register(MeetingParticipant)
class MeetingParticipantAdmin(admin.ModelAdmin):
    list_display = ('meeting', 'employee', 'response_status', 'is_organizer', 'is_optional')
    list_filter = ('response_status', 'is_organizer')


@admin.register(MeetingExternalGuest)
class MeetingExternalGuestAdmin(admin.ModelAdmin):
    list_display = ('meeting', 'email', 'name', 'response_status')
    search_fields = ('email', 'name')


@admin.register(MeetingAuditEvent)
class MeetingAuditEventAdmin(admin.ModelAdmin):
    list_display = ('meeting', 'event_type', 'actor', 'created_at')
    list_filter = ('event_type',)
