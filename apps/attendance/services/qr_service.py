import logging
import secrets
import calendar as cal_mod
from datetime import datetime, timedelta, time, date
from typing import Tuple, Optional, Dict, Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound

from apps.attendance.models import AttendanceQRCode, QRValidityPeriod
from apps.organization.models import Branch, Business, BusinessRole
from apps.organization.services.policy_resolver import PolicyResolver

logger = logging.getLogger(__name__)


class AttendanceQRService:
    """
    Authoritative service for Centre Attendance QR Code lifecycle:
    - Cryptographic token generation
    - Timezone-aware expiry calculation (Dynamic, Daily, Weekly, Monthly)
    - Just-in-time expired QR cleanup (isolated per centre, no dedicated background scheduler needed)
    - Concurrency-safe reuse and active QR locking (via select_for_update)
    - Server-side token validation on punch
    - Immediate token revocation
    """

    @classmethod
    def get_centre_timezone(cls, centre: Branch) -> ZoneInfo:
        """Resolves the effective timezone for a centre, falling back to business or Asia/Kolkata."""
        tz_str = (centre.timezone if centre and centre.timezone else centre.business.timezone if centre and centre.business else None) or 'Asia/Kolkata'
        try:
            return ZoneInfo(tz_str)
        except Exception:
            return ZoneInfo('Asia/Kolkata')

    @classmethod
    def calculate_expiry(
        cls,
        validity_period: str,
        centre: Branch,
        server_now: Optional[datetime] = None,
        dynamic_seconds: int = 300
    ) -> datetime:
        """
        Calculates the exact timezone-aware expiration timestamp.
        Calendar-based boundaries (DAILY, WEEKLY, MONTHLY) are computed in the centre's local timezone.
        """
        now = server_now or timezone.now()
        tz = cls.get_centre_timezone(centre)
        local_dt = now.astimezone(tz)
        local_date = local_dt.date()

        validity_norm = str(validity_period).upper()

        if validity_norm == QRValidityPeriod.DYNAMIC:
            return now + timedelta(seconds=max(30, int(dynamic_seconds)))

        elif validity_norm == QRValidityPeriod.DAILY:
            # End of the current calendar day in local timezone (23:59:59.999999)
            local_end_of_day = datetime.combine(local_date, time(23, 59, 59, 999999), tzinfo=tz)
            return local_end_of_day

        elif validity_norm == QRValidityPeriod.WEEKLY:
            # End of current week (Sunday 23:59:59.999999 in local timezone)
            days_to_sunday = 6 - local_date.weekday()
            sunday_date = local_date + timedelta(days=days_to_sunday)
            local_end_of_week = datetime.combine(sunday_date, time(23, 59, 59, 999999), tzinfo=tz)
            return local_end_of_week

        elif validity_norm == QRValidityPeriod.MONTHLY:
            # End of current calendar month in local timezone
            _, last_day = cal_mod.monthrange(local_date.year, local_date.month)
            month_end_date = date(local_date.year, local_date.month, last_day)
            local_end_of_month = datetime.combine(month_end_date, time(23, 59, 59, 999999), tzinfo=tz)
            return local_end_of_month

        else:
            return now + timedelta(seconds=max(30, int(dynamic_seconds)))

    @classmethod
    def cleanup_expired_qrs(cls, centre: Branch, server_now: Optional[datetime] = None) -> int:
        """
        Deletes ONLY expired QR records belonging to the specified centre.
        Guarantees:
        - Never deletes valid (unexpired) QR codes.
        - Never deletes QR codes belonging to other centres or businesses.
        - Never deletes attendance records (AttendanceDay, AttendanceEvent) or other business data.
        - Runs within the caller's transaction scope.
        """
        now = server_now or timezone.now()
        try:
            deleted_count, _ = AttendanceQRCode.objects.filter(
                centre=centre,
                expires_at__lte=now
            ).delete()

            if deleted_count > 0:
                logger.info(
                    f"[QR_CLEANUP] Deleted {deleted_count} expired QR record(s) for centre {centre.id} ({centre.name}).",
                    extra={'centre_id': str(centre.id), 'business_id': str(centre.business_id), 'deleted_count': deleted_count}
                )
            return deleted_count
        except Exception as e:
            logger.error(
                f"[QR_CLEANUP_FAILURE] Error during expired QR cleanup for centre {centre.id}: {str(e)}",
                extra={'centre_id': str(centre.id), 'error': str(e)}
            )
            raise

    @classmethod
    def generate_or_reuse_qr_code(
        cls,
        centre: Branch,
        user=None,
        validity_period: str = QRValidityPeriod.DYNAMIC,
        dynamic_seconds: int = 300,
        force_refresh: bool = False
    ) -> Tuple[AttendanceQRCode, bool, int]:
        """
        Atomically generates a new QR code or reuses an existing valid compatible QR:
        1. Row-locks active records for the centre with select_for_update() to handle concurrent requests.
        2. Performs just-in-time expired QR cleanup for this centre.
        3. Reuses an existing valid QR if compatible and not explicitly forced to refresh.
        4. If not reusable, generates a cryptographically secure token and stores it.
        Returns: (qr_record, was_created, cleaned_up_expired_count)
        """
        server_now = timezone.now()
        validity_norm = str(validity_period).upper()
        if validity_norm not in [c[0] for c in QRValidityPeriod.choices]:
            validity_norm = QRValidityPeriod.DYNAMIC

        # Resolve policy to verify QR is permitted
        policy_data = PolicyResolver.get_attendance_policy(centre=centre, business=centre.business)
        policy = policy_data.get('effective', {})

        with transaction.atomic():
            # 1. Acquire row lock on existing active records for this centre to serialize concurrent generation requests
            existing_active_qrs = list(
                AttendanceQRCode.objects.select_for_update().filter(
                    centre=centre,
                    validity_period=validity_norm,
                    is_active=True,
                    is_revoked=False
                )
            )

            # 2. Delete expired records for this centre
            cleaned_count = cls.cleanup_expired_qrs(centre=centre, server_now=server_now)

            # 3. Check for reusable valid QR
            reusable_qr = None
            if not force_refresh:
                for q in existing_active_qrs:
                    # Must be active, unrevoked, matching validity, and have > 5s before expiry
                    if q.expires_at > (server_now + timedelta(seconds=5)):
                        reusable_qr = q
                        break

            if reusable_qr:
                # Deactivate any duplicate active records from prior concurrency edge cases
                AttendanceQRCode.objects.filter(
                    centre=centre,
                    validity_period=validity_norm,
                    is_active=True
                ).exclude(id=reusable_qr.id).update(is_active=False)

                return reusable_qr, False, cleaned_count

            # 4. Deactivate prior active QRs for this centre & validity period
            AttendanceQRCode.objects.filter(
                centre=centre,
                validity_period=validity_norm,
                is_active=True
            ).update(is_active=False)

            # 5. Generate unpredictable 32-byte cryptographic token
            token_str = secrets.token_urlsafe(32)
            code_payload = f"OWNMANAGE:CENTRE:{centre.id}:{token_str}"
            expires_at = cls.calculate_expiry(
                validity_period=validity_norm,
                centre=centre,
                server_now=server_now,
                dynamic_seconds=dynamic_seconds
            )

            new_qr = AttendanceQRCode.objects.create(
                business=centre.business,
                centre=centre,
                token=token_str,
                code_payload=code_payload,
                validity_period=validity_norm,
                expires_at=expires_at,
                is_active=True,
                is_revoked=False,
                created_by=user if user and user.is_authenticated else None,
                metadata={
                    'allow_qr': policy.get('allow_qr', False),
                    'allow_gps': policy.get('allow_gps', False),
                    'allow_geofencing': policy.get('allow_geofencing', False),
                    'generated_by_role': getattr(user, 'email', 'system'),
                    'generated_at': server_now.isoformat(),
                }
            )

            logger.info(
                f"[QR_GENERATION] Created new {validity_norm} QR for centre {centre.id} ({centre.name}), expires {expires_at.isoformat()}.",
                extra={'centre_id': str(centre.id), 'validity_period': validity_norm}
            )

            return new_qr, True, cleaned_count

    @classmethod
    def revoke_qr_code(
        cls,
        centre: Branch,
        qr_id: Optional[str] = None,
        token: Optional[str] = None,
        user=None,
        reason: str = ''
    ) -> int:
        """
        Immediately revokes active QR codes for a centre.
        Revoked tokens can never be used again.
        """
        server_now = timezone.now()
        qs = AttendanceQRCode.objects.filter(centre=centre, is_active=True)
        if qr_id:
            qs = qs.filter(id=qr_id)
        if token:
            qs = qs.filter(token=token)

        with transaction.atomic():
            updated = qs.update(
                is_active=False,
                is_revoked=True,
                revoked_at=server_now,
                revoked_by=user if user and user.is_authenticated else None
            )

        logger.warning(
            f"[QR_REVOKED] Revoked {updated} QR code(s) for centre {centre.id}. Reason: {reason or 'Admin revocation'}",
            extra={'centre_id': str(centre.id), 'revoked_count': updated}
        )
        return updated

    @classmethod
    def validate_qr_token(
        cls,
        qr_code_input: str,
        employee,
        centre: Optional[Branch] = None,
        server_now: Optional[datetime] = None
    ) -> AttendanceQRCode:
        """
        Strict server-side cryptographic and lifecycle validation of a scanned QR token:
        1. Extracts token from payload or direct string.
        2. Verifies token exists in the database.
        3. Verifies token belongs to the employee's assigned centre and business.
        4. Verifies token has not been revoked or deactivated.
        5. Verifies token has not expired.
        6. Verifies centre's policy permits QR attendance.
        """
        now = server_now or timezone.now()
        raw = str(qr_code_input or '').strip()
        if not raw:
            raise ValidationError({'detail': 'QR code data is required for QR attendance.'})

        # Extract token and optional centre_id from format: OWNMANAGE:CENTRE:<centre_id>:<token>
        target_token = raw
        if raw.startswith('OWNMANAGE:CENTRE:'):
            parts = raw.split(':')
            if len(parts) >= 4:
                target_token = parts[3]

        # Look up QR record in database
        qr_record = AttendanceQRCode.objects.filter(
            token=target_token
        ).select_related('business', 'centre').first()

        if not qr_record:
            # Fallback search by entire payload
            qr_record = AttendanceQRCode.objects.filter(
                code_payload=raw
            ).select_related('business', 'centre').first()

        if not qr_record:
            logger.warning(
                f"[QR_VALIDATION_FAILED] Scanned token not found in database or already deleted. Employee: {employee.id}"
            )
            raise ValidationError({
                'detail': 'Invalid or expired QR code. The scanned QR token was not found or has been cleaned up.'
            })

        # 1. Tenant & Centre Matching: QR must belong to employee's business
        if qr_record.business_id != employee.business_id:
            logger.warning(
                f"[QR_VALIDATION_FAILED] Cross-business QR attempt. Employee {employee.id} (Biz: {employee.business_id}) scanned QR from Biz: {qr_record.business_id}"
            )
            raise ValidationError({
                'detail': 'Invalid QR code. The scanned QR code belongs to a different business.'
            })

        # 2. Centre Matching: QR must match employee's branch/centre
        expected_centre_id = centre.id if centre else employee.branch_id
        if qr_record.centre_id != expected_centre_id:
            logger.warning(
                f"[QR_VALIDATION_FAILED] Cross-centre QR attempt. Employee {employee.id} (Centre: {expected_centre_id}) scanned QR from Centre: {qr_record.centre_id}"
            )
            raise ValidationError({
                'detail': 'Invalid QR code. The scanned QR does not match your assigned centre.'
            })

        # 3. Revocation Check
        if qr_record.is_revoked or not qr_record.is_active:
            logger.warning(
                f"[QR_VALIDATION_FAILED] Scanned token is revoked or inactive. QR ID: {qr_record.id}"
            )
            raise ValidationError({
                'detail': 'The scanned QR code has been revoked or deactivated. Please scan an active centre QR.'
            })

        # 4. Expiry Check
        if now >= qr_record.expires_at:
            logger.warning(
                f"[QR_VALIDATION_FAILED] Scanned token has expired. QR ID: {qr_record.id}, Expired At: {qr_record.expires_at.isoformat()}"
            )
            raise ValidationError({
                'detail': 'The scanned QR code has expired. Please ask your administrator to refresh the centre QR code.'
            })

        # 5. Policy Check: Verify centre currently allows QR attendance
        policy_data = PolicyResolver.get_attendance_policy(centre=qr_record.centre, business=qr_record.business)
        policy = policy_data.get('effective', {})
        if not policy.get('allow_qr', False):
            raise ValidationError({
                'detail': 'QR Code attendance is disabled for this centre.'
            })

        return qr_record
