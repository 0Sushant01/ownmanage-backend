import uuid
from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class TimeStampedUUIDModel(models.Model):
    """
    Abstract base model providing an immutable UUID primary key
    and creation/modification timestamp tracking.
    """
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        verbose_name=_('ID')
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name=_('Created At')
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name=_('Updated At')
    )

    class Meta:
        abstract = True
        ordering = ['-created_at']


class NotificationType(models.TextChoices):
    LEAVE_STATUS = 'LEAVE_STATUS', _('Leave Status Update')
    PAYROLL_GENERATED = 'PAYROLL_GENERATED', _('Payroll Generated')
    ATTENDANCE_ALERT = 'ATTENDANCE_ALERT', _('Attendance Alert')
    ANNOUNCEMENT = 'ANNOUNCEMENT', _('Company Announcement')
    GENERAL = 'GENERAL', _('General Notification')


class Notification(TimeStampedUUIDModel):
    """
    User notification supporting leave, payroll, attendance, and announcement alerts.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
        verbose_name=_('User')
    )
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='notifications',
        verbose_name=_('Business')
    )
    title = models.CharField(max_length=255, verbose_name=_('Title'))
    message = models.TextField(verbose_name=_('Message'))
    notification_type = models.CharField(
        max_length=50,
        choices=NotificationType.choices,
        default=NotificationType.GENERAL,
        verbose_name=_('Type')
    )
    is_read = models.BooleanField(default=False, verbose_name=_('Is Read'))
    read_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Read At'))

    class Meta:
        verbose_name = _('Notification')
        verbose_name_plural = _('Notifications')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'is_read', '-created_at'], name='idx_notif_user_read_created'),
            models.Index(fields=['business', '-created_at'], name='idx_notif_biz_created'),
        ]

    def __str__(self):
        return f"{self.title} -> {self.user}"


class Announcement(TimeStampedUUIDModel):
    """
    Broadcast announcements scoped per business.
    """
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.CASCADE,
        related_name='announcements',
        verbose_name=_('Business')
    )
    title = models.CharField(max_length=255, verbose_name=_('Title'))
    message = models.TextField(verbose_name=_('Message'))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_announcements',
        verbose_name=_('Created By')
    )
    published_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Published At'))
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Expires At'))
    is_active = models.BooleanField(default=True, verbose_name=_('Is Active'))

    class Meta:
        verbose_name = _('Announcement')
        verbose_name_plural = _('Announcements')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['business', 'is_active', '-published_at'], name='idx_annc_biz_active_pub'),
        ]

    def __str__(self):
        return f"{self.title} ({self.business_id})"


class AuditLog(models.Model):
    """
    Immutable audit trail for tracking critical administrative, payroll,
    and attendance actions across businesses.
    """
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        verbose_name=_('ID')
    )
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='audit_logs',
        verbose_name=_('Business')
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='audit_actions',
        verbose_name=_('Actor')
    )
    action = models.CharField(max_length=100, db_index=True, verbose_name=_('Action'))
    entity_type = models.CharField(max_length=100, db_index=True, verbose_name=_('Entity Type'))
    entity_id = models.CharField(max_length=100, db_index=True, verbose_name=_('Entity ID'))
    old_data = models.JSONField(null=True, blank=True, verbose_name=_('Old Data'))
    new_data = models.JSONField(null=True, blank=True, verbose_name=_('New Data'))
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name=_('IP Address'))
    user_agent = models.TextField(blank=True, verbose_name=_('User Agent'))
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name=_('Created At'))

    class Meta:
        verbose_name = _('Audit Log')
        verbose_name_plural = _('Audit Logs')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['business', '-created_at'], name='idx_audit_biz_created'),
            models.Index(fields=['entity_type', 'entity_id'], name='idx_audit_entity'),
            models.Index(fields=['actor', '-created_at'], name='idx_audit_actor_created'),
        ]

    def __str__(self):
        return f"[{self.action}] {self.entity_type} {self.entity_id} by {self.actor or 'System'}"
