from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.accounts.managers import UserManager
from apps.core.models import TimeStampedUUIDModel


class User(AbstractBaseUser, PermissionsMixin, TimeStampedUUIDModel):
    """
    Custom User model representing global authentication identity.
    Email is the unique login identifier.
    Multi-tenant business roles are separated into BusinessMembership.
    """
    email = models.EmailField(
        _('Email Address'),
        unique=True,
        db_index=True,
        max_length=255,
        error_messages={
            'unique': _('A user with that email already exists.'),
        }
    )
    first_name = models.CharField(_('First Name'), max_length=150, blank=True)
    last_name = models.CharField(_('Last Name'), max_length=150, blank=True)
    phone = models.CharField(_('Phone Number'), max_length=30, blank=True)

    is_active = models.BooleanField(
        _('Active Status'),
        default=True,
        help_text=_('Designates whether this user account should be treated as active.')
    )
    is_staff = models.BooleanField(
        _('Staff Status'),
        default=False,
        help_text=_('Designates whether the user can log into the Django admin site.')
    )
    is_superuser = models.BooleanField(
        _('Superuser Status'),
        default=False,
        help_text=_('Designates that this user has all permissions without explicitly assigning them.')
    )

    objects = UserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = _('User')
        verbose_name_plural = _('Users')
        ordering = ['-created_at']

    def __str__(self):
        return self.email

    @property
    def full_name(self) -> str:
        name = f"{self.first_name} {self.last_name}".strip()
        return name if name else self.email

    def get_full_name(self) -> str:
        return self.full_name

    def get_short_name(self) -> str:
        return self.first_name if self.first_name else self.email


class UserOTPPurpose(models.TextChoices):
    ACTIVATION = 'ACTIVATION', _('Account Activation')
    PASSWORD_RESET = 'PASSWORD_RESET', _('Password Reset')


class UserOTP(TimeStampedUUIDModel):
    """
    Cryptographically hashed, rate-limited, single-use One-Time Passwords
    used strictly for first-time account activation and password resets.
    Plaintext OTPs are never persisted in the database.
    """
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='otps',
        verbose_name=_('User')
    )
    otp_hash = models.CharField(max_length=255, verbose_name=_('Hashed OTP'))
    purpose = models.CharField(
        max_length=30,
        choices=UserOTPPurpose.choices,
        verbose_name=_('Purpose')
    )
    expires_at = models.DateTimeField(verbose_name=_('Expires At'))
    attempts = models.PositiveIntegerField(default=0, verbose_name=_('Failed Attempts'))
    max_attempts = models.PositiveIntegerField(default=3, verbose_name=_('Maximum Allowed Attempts'))
    resend_cooldown_until = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_('Resend Cooldown Until')
    )
    is_used = models.BooleanField(default=False, verbose_name=_('Is Used / Consumed'))

    class Meta:
        verbose_name = _('User OTP')
        verbose_name_plural = _('User OTPs')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'purpose', 'is_used'], name='idx_otp_user_purpose'),
        ]

    def __str__(self):
        return f"OTP for {self.user.email} [{self.purpose}] (Used: {self.is_used})"
