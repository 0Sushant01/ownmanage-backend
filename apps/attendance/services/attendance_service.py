import math
from datetime import datetime, date, time, timedelta
from typing import Dict, Any, Optional
from zoneinfo import ZoneInfo
from decimal import Decimal
from django.utils import timezone
from django.db import transaction
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound

from apps.core.permissions import get_user_context
from apps.organization.models import BusinessRole, Employee, Branch
from apps.organization.services.policy_resolver import PolicyResolver
from apps.organization.services.permission_service import PermissionService
from apps.core.services.audit_service import AuditService
from apps.core.models import AuditLog
from apps.attendance.models import (
    AttendanceDay, AttendanceEvent, AttendanceStatus,
    AttendanceEventType, AttendanceEventSource, AttendanceMethod
)
from apps.attendance.services.attendance_calculation_service import AttendanceCalculationService


def haversine_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates great-circle distance in meters between two GPS coordinates."""
    R = 6371000.0  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


class AttendanceService:
    @staticmethod
    def get_employee_timezone(employee: Employee) -> ZoneInfo:
        biz = employee.business
        tz_str = (employee.branch.timezone if employee.branch and employee.branch.timezone else biz.timezone) or 'Asia/Kolkata'
        try:
            return ZoneInfo(tz_str)
        except Exception:
            return ZoneInfo('Asia/Kolkata')

    @classmethod
    def record_punch(
        cls,
        user,
        punch_type: str,  # 'CHECK_IN' or 'CHECK_OUT'
        attendance_method: str = AttendanceMethod.NORMAL,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        location_accuracy: Optional[float] = None,
        device_id: str = '',
        source: str = AttendanceEventSource.WEB,
        qr_code: Optional[str] = None,
        face_data: Optional[Dict[str, Any]] = None,
        notes: str = '',
        target_employee_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Authoritative unified punch execution:
        1. Identifies and validates employee and centre
        2. Resolves effective attendance policy
        3. Validates that the requested attendance method (NORMAL, QR, FACE) is enabled
        4. Validates location geofence when location verification is enabled
        5. Performs QR / Face verification checks
        6. Safely creates or updates AttendanceDay and AttendanceEvent
        7. Centralizes calculation of status, work duration, and overtime
        """
        # Resolve employee
        if target_employee_id:
            # Authorized manager or admin punching on behalf
            emp = Employee.objects.filter(id=target_employee_id, employment_status='ACTIVE').select_related('business', 'branch').first()
            if not emp:
                raise NotFound('Active employee profile not found.')
        else:
            emp = getattr(user, 'employee_profiles', None)
            emp = emp.filter(employment_status='ACTIVE').select_related('business', 'branch').first() if emp else None
            if not emp:
                raise PermissionDenied('No active employee profile linked to user account.')

        centre = emp.branch
        biz = emp.business
        tz = cls.get_employee_timezone(emp)
        server_now = timezone.now()
        local_date = server_now.astimezone(tz).date()

        # Load effective policy for centre
        policy_data = PolicyResolver.get_attendance_policy(centre=centre, business=biz)
        policy = policy_data.get('effective', {})

        # Normalize method
        method_str = str(attendance_method).upper()
        if method_str not in [AttendanceMethod.NORMAL, AttendanceMethod.QR, AttendanceMethod.FACE]:
            raise ValidationError({'detail': f"Unsupported attendance method '{attendance_method}'."})

        # -----------------------------------------------------------
        # Step 3: Method Validation
        # -----------------------------------------------------------
        if method_str == AttendanceMethod.NORMAL:
            if not policy.get('allow_normal_punch', True):
                raise ValidationError({'detail': 'Normal punch attendance is disabled for this centre. Please use the configured method.'})
        elif method_str == AttendanceMethod.QR:
            if not policy.get('allow_qr', False):
                raise ValidationError({'detail': 'QR Code attendance is disabled for this centre.'})
            if not qr_code:
                raise ValidationError({'detail': 'QR code data is required for QR attendance.'})
            
            from apps.attendance.services.qr_service import AttendanceQRService
            qr_record = AttendanceQRService.validate_qr_token(
                qr_code_input=qr_code,
                employee=emp,
                centre=centre,
                server_now=server_now
            )
            qr_meta = {
                'qr_verified': True,
                'qr_code_id': str(qr_record.id),
                'validity_period': qr_record.validity_period,
            }
        elif method_str == AttendanceMethod.FACE:
            if not policy.get('allow_face_recognition', False):
                raise ValidationError({'detail': 'Face recognition attendance is disabled for this centre.'})
            if not face_data:
                raise ValidationError({'detail': 'Face verification assertion or telemetry is required.'})

            # Inspect for cryptographic verification assertion or telemetry
            assertion = face_data.get('verification_assertion') if isinstance(face_data, dict) and 'verification_assertion' in face_data else face_data
            if isinstance(assertion, dict) and (assertion.get('challenge_id') or assertion.get('nonce')):
                from apps.biometrics.services.verification_service import VerificationService
                face_meta = VerificationService.verify_assertion(assertion, emp, centre)
            else:
                if not isinstance(face_data, dict) or not face_data.get('verified', False):
                    raise ValidationError({'detail': 'Face verification failed or face telemetry was not provided.'})
                face_meta = {
                    'face_verified': True,
                    'confidence': face_data.get('confidence', 0.98),
                    'liveness': face_data.get('liveness', True),
                }

        # -----------------------------------------------------------
        # Step 4: Optional Location Verification
        # -----------------------------------------------------------
        location_required = bool(
            policy.get('location_required_checkin' if punch_type == 'CHECK_IN' else 'location_required_checkout')
            or policy.get('allow_geofencing')
        )
        location_verified = False
        verification_metadata = {
            'method': method_str,
            'source': source,
            'timestamp': server_now.isoformat(),
        }

        if method_str == AttendanceMethod.QR:
            verification_metadata.update(qr_meta)
        elif method_str == AttendanceMethod.FACE:
            verification_metadata.update(face_meta)

        if location_required:
            if latitude is None or longitude is None:
                raise ValidationError({
                    'detail': 'Location verification is required for this centre. Please enable GPS and provide location coordinates.',
                    'location_required': True
                })

            c_lat = policy.get('gps_latitude') or (float(centre.latitude) if centre and centre.latitude is not None else None)
            c_lng = policy.get('gps_longitude') or (float(centre.longitude) if centre and centre.longitude is not None else None)
            c_radius = int(policy.get('gps_radius_meters') or (centre.geofence_radius if centre and centre.geofence_radius else 100))

            if c_lat is not None and c_lng is not None:
                distance = haversine_distance_meters(float(latitude), float(longitude), float(c_lat), float(c_lng))
                if distance > c_radius:
                    raise ValidationError({
                        'detail': f'Attendance rejected: You are outside the permitted centre geofence ({int(distance)}m away, allowed radius is {c_radius}m).',
                        'distance_meters': round(distance, 1),
                        'allowed_radius_meters': c_radius,
                        'location_rejected': True
                    })
                location_verified = True
                verification_metadata['location'] = {
                    'verified': True,
                    'distance_meters': round(distance, 1),
                    'allowed_radius_meters': c_radius,
                    'centre_coordinates': [float(c_lat), float(c_lng)],
                }
            else:
                # Centre coordinates not defined, accept but record
                location_verified = True
                verification_metadata['location'] = {
                    'verified': True,
                    'note': 'Centre coordinates not configured'
                }
        else:
            # Location is not required; if coordinates provided, store distance if available
            if latitude is not None and longitude is not None:
                c_lat = policy.get('gps_latitude') or (float(centre.latitude) if centre and centre.latitude is not None else None)
                c_lng = policy.get('gps_longitude') or (float(centre.longitude) if centre and centre.longitude is not None else None)
                if c_lat is not None and c_lng is not None:
                    dist = haversine_distance_meters(float(latitude), float(longitude), float(c_lat), float(c_lng))
                    verification_metadata['location'] = {
                        'provided': True,
                        'distance_meters': round(dist, 1)
                    }

        # -----------------------------------------------------------
        # Step 5: Execute Punch Transaction Safely
        # -----------------------------------------------------------
        with transaction.atomic():
            day, _ = AttendanceDay.objects.select_for_update().get_or_create(
                business=biz,
                employee=emp,
                attendance_date=local_date,
                defaults={
                    'centre': centre,
                    'status': AttendanceStatus.PRESENT,
                    'attendance_method': method_str,
                    'location_verified': location_verified,
                    'verification_metadata': verification_metadata
                }
            )

            # Strict Attendance Locking: Check if this day is locked or has an expired payroll editing window
            if day.is_locked:
                raise ValidationError({'detail': 'Attendance for this date is locked because the associated payroll period has been finalized.'})

            from apps.payroll.models import Payroll, PayrollStatus
            active_payroll = Payroll.objects.filter(
                employee=emp,
                period_start__lte=local_date,
                period_end__gte=local_date
            ).select_related('payroll_run').first()

            if active_payroll:
                if active_payroll.status in [PayrollStatus.FINALIZED, PayrollStatus.RELEASED, PayrollStatus.PAID]:
                    raise ValidationError({'detail': 'Attendance for this date is locked because the associated payroll period has been finalized.'})
                now = timezone.now()
                if active_payroll.editing_deadline and now > active_payroll.editing_deadline:
                    raise ValidationError({'detail': 'Attendance for this date is locked because the payroll editing window has expired.'})
                if active_payroll.payroll_run and active_payroll.payroll_run.editing_deadline and now > active_payroll.payroll_run.editing_deadline:
                    raise ValidationError({'detail': 'Attendance for this date is locked because the payroll editing window has expired.'})

            # Ensure centre is set
            if not day.centre and centre:
                day.centre = centre

            events = list(day.events.order_by('event_time'))
            last_event = events[-1] if events else None

            if punch_type == 'CHECK_IN':
                if last_event and last_event.event_type == AttendanceEventType.CHECK_IN:
                    raise ValidationError({'detail': 'Already checked in. Please check out first before checking in again.'})

                event = AttendanceEvent.objects.create(
                    business=biz,
                    attendance_day=day,
                    employee=emp,
                    event_type=AttendanceEventType.CHECK_IN,
                    attendance_method=method_str,
                    event_time=server_now,
                    latitude=Decimal(str(latitude)) if latitude is not None else None,
                    longitude=Decimal(str(longitude)) if longitude is not None else None,
                    location_accuracy=location_accuracy,
                    location_verified=location_verified,
                    verification_metadata=verification_metadata,
                    device_id=device_id,
                    source=source,
                    notes=notes
                )
                if not day.check_in:
                    day.check_in = server_now
                day.attendance_method = method_str
                day.location_verified = location_verified or day.location_verified
                day.verification_metadata = {**day.verification_metadata, **verification_metadata}
                day.save(update_fields=[
                    'centre', 'check_in', 'attendance_method',
                    'location_verified', 'verification_metadata', 'updated_at'
                ])

            elif punch_type == 'CHECK_OUT':
                if not day or not last_event or last_event.event_type != AttendanceEventType.CHECK_IN:
                    raise ValidationError({'detail': 'Cannot check out without an active check-in session.'})

                # Calculate session seconds
                session_duration = max(0, int((server_now - last_event.event_time).total_seconds()))

                event = AttendanceEvent.objects.create(
                    business=biz,
                    attendance_day=day,
                    employee=emp,
                    event_type=AttendanceEventType.CHECK_OUT,
                    attendance_method=method_str,
                    event_time=server_now,
                    latitude=Decimal(str(latitude)) if latitude is not None else None,
                    longitude=Decimal(str(longitude)) if longitude is not None else None,
                    location_accuracy=location_accuracy,
                    location_verified=location_verified,
                    verification_metadata=verification_metadata,
                    device_id=device_id,
                    source=source,
                    notes=notes
                )
                day.check_out = server_now
                day.total_work_seconds += session_duration
                day.location_verified = location_verified or day.location_verified
                day.verification_metadata = {**day.verification_metadata, **verification_metadata}
                day.save(update_fields=[
                    'centre', 'check_out', 'total_work_seconds',
                    'location_verified', 'verification_metadata', 'updated_at'
                ])

            # Recalculate status, total hours, and overtime
            AttendanceCalculationService.calculate_daily_attendance(day, effective_policy=policy, save=True)

        return cls.get_today_state(emp)

    @classmethod
    def get_today_state(cls, employee: Employee) -> Dict[str, Any]:
        """Returns the full today attendance state for an employee."""
        tz = cls.get_employee_timezone(employee)
        server_now = timezone.now()
        local_now = server_now.astimezone(tz)
        local_date = local_now.date()

        day = AttendanceDay.objects.filter(
            business=employee.business,
            employee=employee,
            attendance_date=local_date
        ).first()

        events = list(day.events.order_by('event_time')) if day else []
        last_event = events[-1] if events else None
        is_checked_in = (last_event.event_type == AttendanceEventType.CHECK_IN) if last_event else False

        accumulated_seconds = day.total_work_seconds if day else 0
        current_session_seconds = 0
        if is_checked_in and last_event:
            current_session_seconds = max(0, int((server_now - last_event.event_time).total_seconds()))

        first_check_in = next((e for e in events if e.event_type == AttendanceEventType.CHECK_IN), None)
        last_check_out = next((e for e in reversed(events) if e.event_type == AttendanceEventType.CHECK_OUT), None)

        from apps.attendance.serializers import AttendanceEventSerializer
        return {
            'attendance_day_id': str(day.id) if day else None,
            'attendance_date': str(local_date),
            'day_status': day.status if day else 'NOT_MARKED',
            'is_checked_in': is_checked_in,
            'total_work_seconds': accumulated_seconds + current_session_seconds,
            'accumulated_seconds': accumulated_seconds,
            'overtime_seconds': day.overtime_seconds if day else 0,
            'attendance_method': day.attendance_method if day else 'NORMAL',
            'location_verified': day.location_verified if day else False,
            'first_check_in_time': first_check_in.event_time.astimezone(tz).strftime('%I:%M %p') if first_check_in else None,
            'last_check_out_time': last_check_out.event_time.astimezone(tz).strftime('%I:%M %p') if last_check_out else None,
            'last_event_type': last_event.event_type if last_event else None,
            'events': AttendanceEventSerializer(events, many=True).data,
            'is_overridden': day.is_overridden if day else False,
        }

    @classmethod
    def override_attendance(
        cls,
        user,
        attendance_day: AttendanceDay,
        check_in_time: Optional[str] = None,
        check_out_time: Optional[str] = None,
        status: Optional[str] = None,
        overtime_seconds: Optional[int] = None,
        reason: str = '',
        notes: str = ''
    ) -> AttendanceDay:
        """
        Manager / Admin Attendance Override:
        - Requires reason
        - Preserves original values
        - Writes immutable AuditLog entry
        - Updates AttendanceDay with override details
        """
        if not reason or not reason.strip():
            raise ValidationError({'detail': 'A valid reason is required for manual attendance override.'})

        if attendance_day.is_locked:
            raise ValidationError({'detail': 'Attendance for this date is locked because the associated payroll period has been finalized. Please submit an attendance correction request.'})

        from apps.payroll.models import Payroll, PayrollStatus
        active_payroll = Payroll.objects.filter(
            employee=attendance_day.employee,
            period_start__lte=attendance_day.attendance_date,
            period_end__gte=attendance_day.attendance_date
        ).select_related('payroll_run').first()

        if active_payroll:
            if active_payroll.status in [PayrollStatus.FINALIZED, PayrollStatus.RELEASED, PayrollStatus.PAID]:
                raise ValidationError({'detail': 'Attendance for this date is locked because the associated payroll has been finalized. Please submit an attendance correction request.'})
            now = timezone.now()
            if active_payroll.editing_deadline and now > active_payroll.editing_deadline:
                raise ValidationError({'detail': f'Attendance for this date is locked because the payroll editing window closed on {active_payroll.editing_deadline}.'})
            if active_payroll.payroll_run and active_payroll.payroll_run.editing_deadline and now > active_payroll.payroll_run.editing_deadline:
                raise ValidationError({'detail': f'Attendance for this date is locked because the payroll editing window closed on {active_payroll.payroll_run.editing_deadline}.'})

        # Capture old state for audit
        old_data = {
            'status': attendance_day.status,
            'check_in': attendance_day.check_in.isoformat() if attendance_day.check_in else None,
            'check_out': attendance_day.check_out.isoformat() if attendance_day.check_out else None,
            'total_work_seconds': attendance_day.total_work_seconds,
            'overtime_seconds': attendance_day.overtime_seconds,
            'notes': attendance_day.notes,
        }

        # Store originals if this is the first override
        if not attendance_day.is_overridden:
            attendance_day.original_check_in = attendance_day.check_in
            attendance_day.original_check_out = attendance_day.check_out
            attendance_day.original_status = attendance_day.status

        # Apply overrides
        tz = cls.get_employee_timezone(attendance_day.employee)
        att_date = attendance_day.attendance_date

        if check_in_time:
            # Accepts format HH:MM or ISO datetime
            if 'T' in str(check_in_time) or ' ' in str(check_in_time):
                try:
                    attendance_day.check_in = datetime.fromisoformat(str(check_in_time).replace('Z', '+00:00'))
                except Exception:
                    pass
            else:
                try:
                    h, m = [int(x) for x in str(check_in_time).split(':')[:2]]
                    attendance_day.check_in = datetime.combine(att_date, time(h, m), tzinfo=tz)
                except Exception as e:
                    raise ValidationError({'detail': f"Invalid check_in time format: {e}"})

        if check_out_time:
            if 'T' in str(check_out_time) or ' ' in str(check_out_time):
                try:
                    attendance_day.check_out = datetime.fromisoformat(str(check_out_time).replace('Z', '+00:00'))
                except Exception:
                    pass
            else:
                try:
                    h, m = [int(x) for x in str(check_out_time).split(':')[:2]]
                    attendance_day.check_out = datetime.combine(att_date, time(h, m), tzinfo=tz)
                except Exception as e:
                    raise ValidationError({'detail': f"Invalid check_out time format: {e}"})

        if status:
            status_upper = str(status).upper()
            if status_upper not in AttendanceStatus.values:
                raise ValidationError({'detail': f"Invalid status '{status}'."})
            attendance_day.status = status_upper

        if overtime_seconds is not None:
            attendance_day.overtime_seconds = max(0, int(overtime_seconds))

        if notes:
            attendance_day.notes = notes

        # Recalculate duration if check_in and check_out are present
        if attendance_day.check_in and attendance_day.check_out:
            if attendance_day.check_out < attendance_day.check_in:
                raise ValidationError({'detail': 'Check-out cannot precede check-in time.'})
            attendance_day.total_work_seconds = max(0, int((attendance_day.check_out - attendance_day.check_in).total_seconds()))

        attendance_day.is_overridden = True
        attendance_day.overridden_by = user
        attendance_day.overridden_at = timezone.now()
        attendance_day.override_reason = reason.strip()

        attendance_day.save()

        # Audit Log
        AuditService.log(
            user_or_request=user,
            action='ATTENDANCE_OVERRIDE',
            entity_type='AttendanceDay',
            entity_id=str(attendance_day.id),
            old_data=old_data,
            new_data={
                'status': attendance_day.status,
                'check_in': attendance_day.check_in.isoformat() if attendance_day.check_in else None,
                'check_out': attendance_day.check_out.isoformat() if attendance_day.check_out else None,
                'total_work_seconds': attendance_day.total_work_seconds,
                'overtime_seconds': attendance_day.overtime_seconds,
                'reason': reason,
            },
            business=attendance_day.business,
            reason=f"Manager override: {reason}"
        )

        return attendance_day
