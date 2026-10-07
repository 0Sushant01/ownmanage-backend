import uuid
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from apps.core.models import TimeStampedUUIDModel
from apps.organization.models import Employee, Business


class BiometricStatus(models.TextChoices):
    ACTIVE = 'ACTIVE', _('Active')
    REVOKED = 'REVOKED', _('Revoked')
    PENDING = 'PENDING', _('Pending')
    FAILED = 'FAILED', _('Failed')


class DeviceStatus(models.TextChoices):
    ACTIVE = 'ACTIVE', _('Active')
    REVOKED = 'REVOKED', _('Revoked')
    PENDING = 'PENDING', _('Pending')


class DevicePlatform(models.TextChoices):
    ANDROID = 'ANDROID', _('Android')
    IOS = 'IOS', _('iOS')
    WEB = 'WEB', _('Web')


class BiometricAuditEventType(models.TextChoices):
    ENROLLMENT = 'ENROLLMENT', _('Face Enrolled')
    RE_ENROLLMENT = 'RE_ENROLLMENT', _('Face Re-enrolled')
    REVOCATION = 'REVOCATION', _('Face Revoked')
    VERIFICATION_SUCCESS = 'VERIFICATION_SUCCESS', _('Verification Success')
    VERIFICATION_FAILED = 'VERIFICATION_FAILED', _('Verification Failed')
    DEVICE_REGISTERED = 'DEVICE_REGISTERED', _('Device Registered')
    DEVICE_REVOKED = 'DEVICE_REVOKED', _('Device Revoked')
    TEMPLATE_PROVISIONED = 'TEMPLATE_PROVISIONED', _('Template Provisioned to Device')


class EmployeeFaceEnrollment(TimeStampedUUIDModel):
    """
    Dedicated biometric template entity for an employee.
    Stores encrypted L2-normalized 128-D MobileFaceNet embeddings.
    Raw photos are NOT stored directly in this model.
    """
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='face_enrollments',
        verbose_name=_('Employee')
    )
    model_id = models.CharField(
        max_length=100,
        default='OWNMANAGE-MOBILEFACENET-ARCFACE-128-V1',
        verbose_name=_('Model Identifier')
    )
    model_version = models.CharField(
        max_length=20,
        default='1.0.0',
        verbose_name=_('Model Version')
    )
    detector_version = models.CharField(
        max_length=20,
        default='1.0.0',
        verbose_name=_('Detector Version')
    )
    preprocessing_version = models.CharField(
        max_length=20,
        default='1.0.0',
        verbose_name=_('Preprocessing Version')
    )
    embedding_dimension = models.PositiveIntegerField(
        default=128,
        verbose_name=_('Embedding Dimension')
    )
    embedding_format = models.CharField(
        max_length=50,
        default='FLOAT32_L2_NORMALIZED',
        verbose_name=_('Embedding Format')
    )
    # Stored encrypted at rest using server-side AES-GCM / Fernet key
    encrypted_embedding = models.TextField(
        verbose_name=_('Encrypted Embedding Ciphertext')
    )
    status = models.CharField(
        max_length=20,
        choices=BiometricStatus.choices,
        default=BiometricStatus.ACTIVE,
        db_index=True,
        verbose_name=_('Enrollment Status')
    )
    quality_score = models.FloatField(
        default=1.0,
        verbose_name=_('Enrollment Quality Score')
    )
    enrolled_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_('Enrolled At')
    )
    enrolled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='performed_face_enrollments',
        verbose_name=_('Enrolled By User')
    )
    revoked_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_('Revoked At')
    )
    revocation_reason = models.TextField(
        blank=True,
        verbose_name=_('Revocation Reason')
    )

    class Meta:
        verbose_name = _('Employee Face Enrollment')
        verbose_name_plural = _('Employee Face Enrollments')
        indexes = [
            models.Index(fields=['employee', 'status']),
            models.Index(fields=['model_id', 'status']),
        ]

    def __str__(self):
        return f"{self.employee} - {self.model_id} ({self.status})"


class EmployeeDevice(TimeStampedUUIDModel):
    """
    Authorized mobile device entity bound to an employee.
    Controls hardware-bound biometric template provisioning and assertion signing.
    """
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='devices',
        verbose_name=_('Employee')
    )
    device_id = models.CharField(
        max_length=150,
        db_index=True,
        verbose_name=_('Device Unique Identifier')
    )
    device_name = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_('Device Name / Model')
    )
    platform = models.CharField(
        max_length=20,
        choices=DevicePlatform.choices,
        default=DevicePlatform.ANDROID,
        verbose_name=_('Platform')
    )
    public_key = models.TextField(
        blank=True,
        verbose_name=_('Device Public Key / Secret for Assertion Verification')
    )
    status = models.CharField(
        max_length=20,
        choices=DeviceStatus.choices,
        default=DeviceStatus.ACTIVE,
        db_index=True,
        verbose_name=_('Device Status')
    )
    registered_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_('Registered At')
    )
    last_seen_at = models.DateTimeField(
        auto_now=True,
        verbose_name=_('Last Seen At')
    )
    revoked_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_('Revoked At')
    )
    revocation_reason = models.TextField(
        blank=True,
        verbose_name=_('Revocation Reason')
    )

    class Meta:
        verbose_name = _('Employee Device')
        verbose_name_plural = _('Employee Devices')
        constraints = [
            models.UniqueConstraint(fields=['employee', 'device_id'], name='unique_employee_device_id')
        ]
        indexes = [
            models.Index(fields=['device_id', 'status']),
        ]

    def __str__(self):
        return f"{self.employee} - {self.device_name or self.device_id} ({self.status})"


class BiometricChallenge(TimeStampedUUIDModel):
    """
    Short-lived cryptographic nonce issued to prevent replay attacks on biometric attendance.
    """
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='biometric_challenges',
        verbose_name=_('Employee')
    )
    device = models.ForeignKey(
        EmployeeDevice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='challenges',
        verbose_name=_('Device')
    )
    nonce = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        verbose_name=_('Cryptographic Nonce')
    )
    issued_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_('Issued At')
    )
    expires_at = models.DateTimeField(
        db_index=True,
        verbose_name=_('Expires At')
    )
    is_used = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name=_('Is Nonce Used')
    )
    used_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_('Used At')
    )

    class Meta:
        verbose_name = _('Biometric Challenge')
        verbose_name_plural = _('Biometric Challenges')

    def __str__(self):
        return f"Challenge {self.nonce[:8]}... for {self.employee} (Used: {self.is_used})"


class BiometricAuditEvent(TimeStampedUUIDModel):
    """
    Immutable audit log for biometric enrollment, revocation, provisioning, and verification attempts.
    Does NOT store raw photographs or plain embeddings.
    """
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name='biometric_audit_events',
        verbose_name=_('Business')
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='biometric_audit_events',
        verbose_name=_('Employee')
    )
    event_type = models.CharField(
        max_length=30,
        choices=BiometricAuditEventType.choices,
        db_index=True,
        verbose_name=_('Event Type')
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='biometric_actions',
        verbose_name=_('Actor / User')
    )
    details = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_('Audit Details Metadata')
    )
    ip_address = models.CharField(
        max_length=45,
        blank=True,
        verbose_name=_('IP Address')
    )
    user_agent = models.TextField(
        blank=True,
        verbose_name=_('User Agent')
    )

    class Meta:
        verbose_name = _('Biometric Audit Event')
        verbose_name_plural = _('Biometric Audit Events')
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.event_type}] {self.employee} at {self.created_at}"
