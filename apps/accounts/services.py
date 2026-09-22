import logging
import secrets
from datetime import timedelta
from django.conf import settings
from django.core.mail import send_mail
from django.contrib.auth.hashers import make_password, check_password
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.models import User, UserOTP, UserOTPPurpose

logger = logging.getLogger(__name__)


def generate_and_send_otp(user: User, purpose: str) -> dict:
    """
    Generates a cryptographically random 6-digit numeric OTP,
    hashes it securely, saves the expiration and rate limit,
    and dispatches via transactional email.
    """
    now = timezone.now()

    # Rate limiting: enforce resend cooldown (60 seconds)
    recent_otp = UserOTP.objects.filter(
        user=user,
        purpose=purpose,
        is_used=False,
        resend_cooldown_until__gt=now
    ).first()

    if recent_otp:
        remaining = int((recent_otp.resend_cooldown_until - now).total_seconds())
        raise ValidationError({
            'detail': f'Please wait {remaining} seconds before requesting a new verification code.'
        })

    # Invalidate previous unused OTPs for this purpose
    UserOTP.objects.filter(user=user, purpose=purpose, is_used=False).update(is_used=True)

    # Cryptographically secure 6-digit OTP
    raw_code = f"{secrets.randbelow(900000) + 100000}"
    hashed_code = make_password(raw_code)

    otp_record = UserOTP.objects.create(
        user=user,
        otp_hash=hashed_code,
        purpose=purpose,
        expires_at=now + timedelta(minutes=10),
        resend_cooldown_until=now + timedelta(seconds=60),
        is_used=False,
    )

    # Dispatch email
    purpose_label = "Account Activation" if purpose == UserOTPPurpose.ACTIVATION else "Password Reset"
    subject = f"OwnManage — {purpose_label} Verification Code"
    message = (
        f"Hello {user.full_name or user.email},\n\n"
        f"Your verification code for {purpose_label.lower()} is:\n\n"
        f"    {raw_code}\n\n"
        f"This code is valid for 10 minutes and can only be used once.\n"
        f"If you did not request this, please disregard this email.\n\n"
        f"— OwnManage Security Team"
    )

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
    except Exception as e:
        logger.warning(f"Failed to dispatch OTP email to {user.email}: {e}")
        # In development / console testing, log the code
        print(f"\n==================== [DEV OTP NOTICE] ====================")
        print(f"To: {user.email} | Purpose: {purpose} | Code: {raw_code}")
        print(f"==========================================================\n")

    return {
        'detail': f'Verification code sent to {user.email}.',
        'cooldown_seconds': 60,
    }


def verify_and_consume_otp(user: User, otp_code: str, purpose: str) -> bool:
    """
    Validates an incoming OTP code against the hashed record.
    Enforces expiration, single-use, and maximum failed attempt thresholds.
    """
    now = timezone.now()

    otp_record = UserOTP.objects.filter(
        user=user,
        purpose=purpose,
        is_used=False
    ).order_by('-created_at').first()

    if not otp_record:
        raise ValidationError({'detail': 'No active verification request found. Please request a new code.'})

    if otp_record.expires_at < now:
        otp_record.is_used = True
        otp_record.save(update_fields=['is_used'])
        raise ValidationError({'detail': 'Verification code has expired. Please request a new one.'})

    if otp_record.attempts >= otp_record.max_attempts:
        otp_record.is_used = True
        otp_record.save(update_fields=['is_used'])
        raise ValidationError({'detail': 'Maximum verification attempts exceeded. Please request a new code.'})

    is_valid = check_password(otp_code.strip(), otp_record.otp_hash)

    if not is_valid:
        otp_record.attempts += 1
        otp_record.save(update_fields=['attempts'])
        remaining_attempts = max(0, otp_record.max_attempts - otp_record.attempts)
        raise ValidationError({
            'detail': f'Invalid verification code. {remaining_attempts} attempt(s) remaining.'
        })

    # Mark consumed
    otp_record.is_used = True
    otp_record.save(update_fields=['is_used', 'updated_at'])
    return True
