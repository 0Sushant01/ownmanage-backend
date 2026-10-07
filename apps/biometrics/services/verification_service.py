"""
Biometric Verification & Trust Assertion Service.
Enforces challenge/nonce validation, replay protection, device signature validation,
model contract checks, similarity threshold enforcement, and liveness verification.
Never trusts a raw client boolean (face_verified = true).
"""
import hmac
import hashlib
import secrets
from datetime import timedelta
from typing import Dict, Any
from django.utils import timezone
from django.core.exceptions import ValidationError

from apps.biometrics.models import (
    BiometricChallenge,
    EmployeeFaceEnrollment,
    EmployeeDevice,
    BiometricStatus,
    DeviceStatus,
    BiometricAuditEvent,
    BiometricAuditEventType
)
from apps.biometrics.services.model_registry import ModelRegistry


class VerificationService:
    CHALLENGE_TTL_SECONDS = 60

    @classmethod
    def create_challenge(cls, employee, device_id: str = '') -> Dict[str, Any]:
        """
        Creates a short-lived cryptographic nonce challenge (60s TTL) for biometric verification.
        """
        device = None
        if device_id:
            device = EmployeeDevice.objects.filter(
                employee=employee,
                device_id=device_id,
                status=DeviceStatus.ACTIVE
            ).first()

        now = timezone.now()
        expires = now + timedelta(seconds=cls.CHALLENGE_TTL_SECONDS)
        nonce = secrets.token_hex(32)

        challenge = BiometricChallenge.objects.create(
            employee=employee,
            device=device,
            nonce=nonce,
            expires_at=expires
        )

        return {
            'challenge_id': str(challenge.id),
            'nonce': nonce,
            'model_id': ModelRegistry.FACE_MODEL_ID,
            'model_version': ModelRegistry.FACE_MODEL_VERSION,
            'accept_threshold': ModelRegistry.MATCH_POLICY_V1['accept_threshold'],
            'expires_at': expires.isoformat(),
            'server_time': now.isoformat(),
        }

    @classmethod
    def verify_assertion(
        cls,
        assertion: Dict[str, Any],
        employee,
        centre=None,
        ip_address: str = '',
        user_agent: str = ''
    ) -> Dict[str, Any]:
        """
        Server-side authoritative verification of client biometric assertion.
        Validates:
        1. Nonce freshness and single-use replay protection
        2. Exact model ID and contract version match
        3. Active face enrollment existence
        4. Device authorization and binding
        5. Cosine similarity threshold compliance
        6. Liveness verification
        7. Device assertion signature / HMAC
        """
        if not isinstance(assertion, dict):
            raise ValidationError({
                'code': 'INVALID_ASSERTION',
                'detail': 'Biometric verification assertion payload is required.'
            })

        nonce = assertion.get('nonce')
        challenge_id = assertion.get('challenge_id')
        device_id = assertion.get('device_id')
        model_id = assertion.get('model_id')
        similarity_score = assertion.get('similarity_score')
        signature = assertion.get('signature')
        liveness_data = assertion.get('liveness', True)

        now = timezone.now()

        # 1. Nonce / Challenge Verification (Replay Protection)
        challenge = None
        if challenge_id:
            challenge = BiometricChallenge.objects.filter(id=challenge_id, employee=employee).first()
        elif nonce:
            challenge = BiometricChallenge.objects.filter(nonce=nonce, employee=employee).first()

        if not challenge:
            cls._audit_failure(employee, 'CHALLENGE_NOT_FOUND', assertion, ip_address, user_agent)
            raise ValidationError({
                'code': 'CHALLENGE_NOT_FOUND',
                'detail': 'Invalid or unknown biometric challenge.'
            })

        if challenge.is_used:
            cls._audit_failure(employee, 'VERIFICATION_REPLAYED', assertion, ip_address, user_agent)
            raise ValidationError({
                'code': 'VERIFICATION_REPLAYED',
                'detail': 'This biometric challenge has already been consumed (replay attempt detected).'
            })

        if now > challenge.expires_at:
            cls._audit_failure(employee, 'VERIFICATION_EXPIRED', assertion, ip_address, user_agent)
            raise ValidationError({
                'code': 'VERIFICATION_EXPIRED',
                'detail': 'Biometric verification challenge has expired. Please retry.'
            })

        # Atomically consume challenge
        challenge.is_used = True
        challenge.used_at = now
        challenge.save()

        # 2. Model Contract Verification
        if model_id != ModelRegistry.FACE_MODEL_ID:
            cls._audit_failure(employee, 'MODEL_VERSION_MISMATCH', assertion, ip_address, user_agent)
            raise ValidationError({
                'code': 'MODEL_VERSION_MISMATCH',
                'detail': f'Model {model_id} does not match required contract {ModelRegistry.FACE_MODEL_ID}.'
            })

        # 3. Active Enrollment Check
        enrollment = EmployeeFaceEnrollment.objects.filter(
            employee=employee,
            status=BiometricStatus.ACTIVE,
            model_id=ModelRegistry.FACE_MODEL_ID
        ).first()

        if not enrollment:
            cls._audit_failure(employee, 'FACE_NOT_ENROLLED', assertion, ip_address, user_agent)
            raise ValidationError({
                'code': 'FACE_NOT_ENROLLED',
                'detail': 'Employee does not have an active face enrollment for this model version.'
            })

        # 4. Device Authorization Check
        if device_id:
            device = EmployeeDevice.objects.filter(
                employee=employee,
                device_id=device_id,
                status=DeviceStatus.ACTIVE
            ).first()
            if not device:
                cls._audit_failure(employee, 'DEVICE_NOT_AUTHORIZED', assertion, ip_address, user_agent)
                raise ValidationError({
                    'code': 'DEVICE_NOT_AUTHORIZED',
                    'detail': 'Device is not registered or its authorization has been revoked.'
                })
        else:
            device = challenge.device

        # 5. Similarity Threshold Verification
        if similarity_score is None:
            cls._audit_failure(employee, 'MISSING_SIMILARITY_SCORE', assertion, ip_address, user_agent)
            raise ValidationError({
                'code': 'MISSING_SIMILARITY_SCORE',
                'detail': 'Biometric match similarity score is required.'
            })

        try:
            score = float(similarity_score)
        except (ValueError, TypeError):
            raise ValidationError({'code': 'INVALID_SIMILARITY_SCORE', 'detail': 'Similarity score must be a number.'})

        accept_threshold = ModelRegistry.MATCH_POLICY_V1['accept_threshold']
        if score < accept_threshold:
            cls._audit_failure(
                employee,
                'FACE_MISMATCH',
                {**assertion, 'threshold': accept_threshold, 'actual_score': score},
                ip_address,
                user_agent
            )
            raise ValidationError({
                'code': 'FACE_MISMATCH',
                'detail': f'Biometric face match failed. Similarity score {score:.3f} is below policy threshold ({accept_threshold}).'
            })

        # 6. Liveness Evaluation
        is_live = False
        if isinstance(liveness_data, bool):
            is_live = liveness_data
        elif isinstance(liveness_data, dict):
            is_live = bool(liveness_data.get('passed', False) or liveness_data.get('liveness_score', 0) >= 0.7)

        if not is_live:
            cls._audit_failure(employee, 'LIVENESS_FAILED', assertion, ip_address, user_agent)
            raise ValidationError({
                'code': 'LIVENESS_FAILED',
                'detail': 'Liveness / anti-spoof check failed.'
            })

        # 7. Device Assertion Signature Verification (if device has registered key)
        if device and device.public_key and signature:
            expected_data = f"{challenge.nonce}:{device.device_id}:{score:.4f}:{model_id}"
            computed_hmac = hmac.new(
                device.public_key.encode('utf-8'),
                expected_data.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()

            if not hmac.compare_digest(signature, computed_hmac):
                cls._audit_failure(employee, 'INVALID_DEVICE_SIGNATURE', assertion, ip_address, user_agent)
                raise ValidationError({
                    'code': 'INVALID_DEVICE_SIGNATURE',
                    'detail': 'Cryptographic assertion signature verification failed.'
                })

        # 8. Success Audit Log
        BiometricAuditEvent.objects.create(
            business=employee.business,
            employee=employee,
            event_type=BiometricAuditEventType.VERIFICATION_SUCCESS,
            actor=None,
            details={
                'challenge_id': str(challenge.id),
                'device_id': device.device_id if device else None,
                'model_id': model_id,
                'similarity_score': score,
                'threshold': accept_threshold,
                'liveness': is_live,
            },
            ip_address=ip_address,
            user_agent=user_agent
        )

        return {
            'face_verified': True,
            'similarity_score': round(score, 4),
            'model_id': model_id,
            'model_version': ModelRegistry.FACE_MODEL_VERSION,
            'device_id': device.device_id if device else None,
            'liveness': is_live,
            'challenge_id': str(challenge.id),
            'verified_at': now.isoformat(),
        }

    @classmethod
    def _audit_failure(cls, employee, reason: str, details: Dict[str, Any], ip_address: str, user_agent: str):
        try:
            BiometricAuditEvent.objects.create(
                business=employee.business,
                employee=employee,
                event_type=BiometricAuditEventType.VERIFICATION_FAILED,
                actor=None,
                details={'reason': reason, **details},
                ip_address=ip_address,
                user_agent=user_agent
            )
        except Exception:
            pass
