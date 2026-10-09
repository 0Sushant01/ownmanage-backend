import uuid
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _
from apps.core.models import TimeStampedUUIDModel


class MeetingStatus(models.TextChoices):
    DRAFT = 'DRAFT', _('Draft')
    SCHEDULED = 'SCHEDULED', _('Scheduled')
    COMPLETED = 'COMPLETED', _('Completed')
    CANCELLED = 'CANCELLED', _('Cancelled')


class MeetingLocationType(models.TextChoices):
    ONLINE = 'ONLINE', _('Online / Video Call')
    IN_PERSON = 'IN_PERSON', _('In Person')
    OTHER = 'OTHER', _('Other / Custom')


class ParticipantResponseStatus(models.TextChoices):
    PENDING = 'PENDING', _('Pending')
    ACCEPTED = 'ACCEPTED', _('Accepted')
    DECLINED = 'DECLINED', _('Declined')
    TENTATIVE = 'TENTATIVE', _('Tentative')


class AuditEventType(models.TextChoices):
    CREATED = 'CREATED', _('Meeting Created')
    RESCHEDULED = 'RESCHEDULED', _('Meeting Rescheduled')
    UPDATED = 'UPDATED', _('Meeting Details Updated')
    CANCELLED = 'CANCELLED', _('Meeting Cancelled')
    PARTICIPANT_ADDED = 'PARTICIPANT_ADDED', _('Participant Added')
    PARTICIPANT_REMOVED = 'PARTICIPANT_REMOVED', _('Participant Removed')
    RESPONSE_CHANGED = 'RESPONSE_CHANGED', _('RSVP Response Changed')


class Meeting(TimeStampedUUIDModel):
    """
    Core meeting entity multi-tenant scoped to a Business.
    Supports centre/branch scoping, rich location metadata, independent lifecycle status,
    and strict logical separation from attendance and leave records.
    """
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.CASCADE,
        related_name='meetings',
        verbose_name=_('Business / Tenant')
    )
    branch = models.ForeignKey(
        'organization.Branch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='meetings',
        verbose_name=_('Centre / Branch Scope')
    )
    title = models.CharField(max_length=255, verbose_name=_('Meeting Title'))
    description = models.TextField(blank=True, verbose_name=_('Agenda / Description'))
    meeting_date = models.DateField(db_index=True, verbose_name=_('Meeting Date'))
    start_time = models.TimeField(verbose_name=_('Start Time'))
    end_time = models.TimeField(verbose_name=_('End Time'))
    timezone = models.CharField(max_length=50, default='Asia/Kolkata', verbose_name=_('Timezone'))

    location_type = models.CharField(
        max_length=30,
        choices=MeetingLocationType.choices,
        default=MeetingLocationType.ONLINE,
        verbose_name=_('Location Type')
    )
    location_details = models.CharField(max_length=255, blank=True, verbose_name=_('Location Details / Room'))
    meeting_url = models.URLField(max_length=500, blank=True, null=True, verbose_name=_('Online Meeting Link'))

    organizer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='organized_meetings',
        verbose_name=_('Organizer User')
    )
    organizer_employee = models.ForeignKey(
        'organization.Employee',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='organized_meetings',
        verbose_name=_('Organizer Employee Profile')
    )

    status = models.CharField(
        max_length=30,
        choices=MeetingStatus.choices,
        default=MeetingStatus.SCHEDULED,
        db_index=True,
        verbose_name=_('Lifecycle Status')
    )
    cancellation_reason = models.TextField(blank=True, verbose_name=_('Cancellation Reason'))
    cancelled_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Cancelled At'))
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cancelled_meetings',
        verbose_name=_('Cancelled By')
    )

    class Meta:
        verbose_name = _('Meeting')
        verbose_name_plural = _('Meetings')
        ordering = ['meeting_date', 'start_time']
        indexes = [
            models.Index(fields=['business', 'meeting_date', 'status'], name='idx_meet_biz_date_status'),
            models.Index(fields=['business', 'organizer', 'meeting_date'], name='idx_meet_biz_org_date'),
            models.Index(fields=['business', 'branch', 'meeting_date'], name='idx_meet_biz_branch_date'),
        ]

    def __str__(self):
        return f"{self.title} on {self.meeting_date} ({self.get_status_display()})"


class MeetingParticipant(TimeStampedUUIDModel):
    """
    Internal employee invited to a meeting.
    Response status is tracked independently per participant.
    """
    meeting = models.ForeignKey(
        Meeting,
        on_delete=models.CASCADE,
        related_name='participants',
        verbose_name=_('Meeting')
    )
    employee = models.ForeignKey(
        'organization.Employee',
        on_delete=models.CASCADE,
        related_name='meeting_participations',
        verbose_name=_('Internal Employee')
    )
    response_status = models.CharField(
        max_length=30,
        choices=ParticipantResponseStatus.choices,
        default=ParticipantResponseStatus.PENDING,
        db_index=True,
        verbose_name=_('RSVP Response Status')
    )
    response_note = models.CharField(max_length=255, blank=True, verbose_name=_('Response Note'))
    responded_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Responded At'))
    is_organizer = models.BooleanField(default=False, verbose_name=_('Is Organizer'))
    is_optional = models.BooleanField(default=False, verbose_name=_('Is Optional Invitee'))

    class Meta:
        verbose_name = _('Meeting Participant')
        verbose_name_plural = _('Meeting Participants')
        constraints = [
            models.UniqueConstraint(
                fields=['meeting', 'employee'],
                name='unique_meeting_participant'
            )
        ]
        indexes = [
            models.Index(fields=['meeting', 'response_status'], name='idx_part_meet_status'),
            models.Index(fields=['employee', 'response_status'], name='idx_part_emp_status'),
        ]

    def __str__(self):
        return f"{self.employee.full_name} -> {self.meeting.title} ({self.get_response_status_display()})"


class MeetingExternalGuest(TimeStampedUUIDModel):
    """
    External guest invited via email.
    Completely isolated: never creates User or Employee accounts. Zero access to system.
    """
    meeting = models.ForeignKey(
        Meeting,
        on_delete=models.CASCADE,
        related_name='external_guests',
        verbose_name=_('Meeting')
    )
    email = models.EmailField(db_index=True, verbose_name=_('Guest Email Address'))
    name = models.CharField(max_length=150, blank=True, verbose_name=_('Guest Name'))
    response_status = models.CharField(
        max_length=30,
        choices=ParticipantResponseStatus.choices,
        default=ParticipantResponseStatus.PENDING,
        verbose_name=_('RSVP Response Status')
    )
    responded_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Responded At'))
    invitation_token = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        verbose_name=_('Cryptographic Invitation Token')
    )

    class Meta:
        verbose_name = _('Meeting External Guest')
        verbose_name_plural = _('Meeting External Guests')
        constraints = [
            models.UniqueConstraint(
                fields=['meeting', 'email'],
                name='unique_meeting_external_guest'
            )
        ]

    def __str__(self):
        return f"{self.email} -> {self.meeting.title} ({self.get_response_status_display()})"


class MeetingAuditEvent(TimeStampedUUIDModel):
    """
    Historical log of lifecycle transitions, rescheduling, and participant changes.
    """
    meeting = models.ForeignKey(
        Meeting,
        on_delete=models.CASCADE,
        related_name='audit_events',
        verbose_name=_('Meeting')
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_('Actor')
    )
    event_type = models.CharField(
        max_length=50,
        choices=AuditEventType.choices,
        verbose_name=_('Event Type')
    )
    summary = models.CharField(max_length=255, verbose_name=_('Summary'))
    diff_data = models.JSONField(default=dict, blank=True, verbose_name=_('Diff / Snapshot Data'))

    class Meta:
        verbose_name = _('Meeting Audit Event')
        verbose_name_plural = _('Meeting Audit Events')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['meeting', '-created_at'], name='idx_meet_audit_created'),
        ]

    def __str__(self):
        return f"[{self.event_type}] {self.meeting.title}: {self.summary}"


class MeetingNotificationOutbox(TimeStampedUUIDModel):
    """
    Pre-configured event outbox for future email notification phase.
    Stores dispatch metadata without sending any emails in the current phase.
    """
    meeting = models.ForeignKey(
        Meeting,
        on_delete=models.CASCADE,
        related_name='notification_outboxes',
        verbose_name=_('Meeting')
    )
    recipient_email = models.EmailField(verbose_name=_('Recipient Email'))
    recipient_type = models.CharField(
        max_length=30,
        choices=[
            ('INTERNAL_EMPLOYEE', _('Internal Employee')),
            ('EXTERNAL_GUEST', _('External Guest')),
        ],
        verbose_name=_('Recipient Type')
    )
    event_type = models.CharField(max_length=50, verbose_name=_('Event Type'))
    status = models.CharField(
        max_length=30,
        default='PENDING_FUTURE_DISPATCH',
        verbose_name=_('Dispatch Status')
    )
    payload = models.JSONField(default=dict, blank=True, verbose_name=_('Email Payload'))

    class Meta:
        verbose_name = _('Meeting Notification Outbox')
        verbose_name_plural = _('Meeting Notification Outboxes')
        ordering = ['-created_at']
