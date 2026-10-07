"""
Employee Device Authorization & Lifecycle Service.
Binds employees to authorized hardware devices to prevent unauthorized device access
and verify biometric attendance assertions.
"""
from django.utils import timezone
from django.core.exceptions import ValidationError
from apps.biometrics.models import (
    EmployeeDevice,
    DeviceStatus,
    BiometricAuditEvent,
    BiometricAuditEventType
)


class DeviceService:
    @classmethod
    def register_device(
        cls,
        employee,
        device_id: str,
        device_name: str = '',
        platform: str = 'ANDROID',
        public_key: str = '',
        actor=None,
        ip_address: str = '',
        user_agent: str = ''
    ) -> EmployeeDevice:
        """
        Registers or reactivates an authorized mobile device for an employee.
        Enforces a single active device policy per employee by revoking older devices.
        """
        if not device_id or not device_id.strip():
            raise ValidationError({'code': 'INVALID_DEVICE_ID', 'detail': 'device_id cannot be blank.'})

        now = timezone.now()

        # Revoke other active devices for this employee to prevent multi-device sharing
        other_devices = EmployeeDevice.objects.filter(
            employee=employee,
            status=DeviceStatus.ACTIVE
        ).exclude(device_id=device_id)

        for dev in other_devices:
            dev.status = DeviceStatus.REVOKED
            dev.revoked_at = now
            dev.revocation_reason = f"Superceded by new device {device_name or device_id} registered at {now.isoformat()}"
            dev.save()

        # Create or update current device
        device, created = EmployeeDevice.objects.get_or_create(
            employee=employee,
            device_id=device_id,
            defaults={
                'device_name': device_name,
                'platform': platform.upper(),
                'public_key': public_key,
                'status': DeviceStatus.ACTIVE,
            }
        )

        if not created:
            device.device_name = device_name or device.device_name
            device.platform = platform.upper()
            device.public_key = public_key or device.public_key
            device.status = DeviceStatus.ACTIVE
            device.revoked_at = None
            device.revocation_reason = ''
            device.save()

        BiometricAuditEvent.objects.create(
            business=employee.business,
            employee=employee,
            event_type=BiometricAuditEventType.DEVICE_REGISTERED,
            actor=actor,
            details={
                'device_id': device_id,
                'device_name': device_name,
                'platform': platform,
                'is_new': created
            },
            ip_address=ip_address,
            user_agent=user_agent
        )

        return device

    @classmethod
    def revoke_device(
        cls,
        employee,
        device_id: str,
        reason: str = '',
        actor=None,
        ip_address: str = '',
        user_agent: str = ''
    ) -> bool:
        """
        Revokes device authorization for an employee.
        """
        device = EmployeeDevice.objects.filter(
            employee=employee,
            device_id=device_id,
            status=DeviceStatus.ACTIVE
        ).first()

        if not device:
            return False

        now = timezone.now()
        device.status = DeviceStatus.REVOKED
        device.revoked_at = now
        device.revocation_reason = reason or f"Revoked by {actor.get_full_name() if actor else 'User'}"
        device.save()

        BiometricAuditEvent.objects.create(
            business=employee.business,
            employee=employee,
            event_type=BiometricAuditEventType.DEVICE_REVOKED,
            actor=actor,
            details={'device_id': device_id, 'reason': reason},
            ip_address=ip_address,
            user_agent=user_agent
        )
        return True

    @classmethod
    def get_active_device(cls, employee, device_id: str) -> EmployeeDevice:
        """
        Validates that a device is active and bound to the employee.
        """
        device = EmployeeDevice.objects.filter(
            employee=employee,
            device_id=device_id,
            status=DeviceStatus.ACTIVE
        ).first()

        if not device:
            raise ValidationError({
                'code': 'DEVICE_NOT_AUTHORIZED',
                'detail': 'Device is not registered or authorization has been revoked.'
            })
        return device
