from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from apps.accounts.models import User
from apps.organization.models import (
    Business, BusinessMembership, BusinessRole,
    Branch, Department, Employee
)
from apps.attendance.models import (
    AttendancePolicy, AttendancePolicyOverride, AttendanceDay,
    AttendanceEvent, AttendanceStatus, AttendanceMethod, AttendanceEventType
)
from apps.attendance.services.attendance_service import AttendanceService, haversine_distance_meters
from apps.attendance.services.attendance_calculation_service import AttendanceCalculationService
from apps.organization.services.policy_resolver import PolicyResolver
from apps.core.models import AuditLog


class AttendanceUnifiedSystemTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.today = date.today()

        # Business
        self.biz = Business.objects.create(
            name='Attendance Corp',
            timezone='Asia/Kolkata',
            currency='INR'
        )

        # Enterprise Policy
        self.ent_policy = AttendancePolicy.objects.create(
            business=self.biz,
            office_start=time(0, 0),
            office_end=time(23, 59),
            grace_period_minutes=1440,
            minimum_present_minutes=480,
            minimum_half_day_minutes=240,
            ot_enabled=False,
            ot_grace_minutes=30,
            allow_normal_punch=True,
            allow_qr=True,
            allow_face_recognition=True,
            location_required_checkin=False,
            location_required_checkout=False,
            allow_geofencing=False
        )

        # Centre 1: Mumbai (GPS coordinates: 19.076000, 72.877700, radius 100m)
        self.centre_mumbai = Branch.objects.create(
            business=self.biz,
            name='Mumbai Central',
            code='MUM-01',
            latitude=Decimal('19.076000'),
            longitude=Decimal('72.877700'),
            geofence_radius=100,
            status='ACTIVE'
        )

        # Centre 2: Delhi (QR only, location mandatory)
        self.centre_delhi = Branch.objects.create(
            business=self.biz,
            name='Delhi North',
            code='DEL-01',
            latitude=Decimal('28.704100'),
            longitude=Decimal('77.102500'),
            geofence_radius=150,
            status='ACTIVE'
        )
        self.delhi_override = AttendancePolicyOverride.objects.create(
            centre=self.centre_delhi,
            allow_normal_punch=False,
            allow_qr=True,
            allow_face_recognition=False,
            location_required_checkin=True,
            location_required_checkout=True,
            gps_latitude=Decimal('28.704100'),
            gps_longitude=Decimal('77.102500'),
            gps_radius_meters=150,
            ot_enabled=True,
            ot_grace_minutes=30
        )

        # Users & Employees
        # Admin
        self.admin_user = User.objects.create_user(
            email='admin@attendancecorp.com',
            password='Password123!',
            first_name='Admin',
            last_name='User'
        )
        BusinessMembership.objects.create(
            business=self.biz,
            user=self.admin_user,
            role=BusinessRole.BUSINESS_ADMIN
        )

        # Manager
        self.manager_user = User.objects.create_user(
            email='manager@attendancecorp.com',
            password='Password123!',
            first_name='Manager',
            last_name='User'
        )
        BusinessMembership.objects.create(
            business=self.biz,
            user=self.manager_user,
            role=BusinessRole.MANAGER
        )
        self.manager_emp = Employee.objects.create(
            business=self.biz,
            user=self.manager_user,
            branch=self.centre_mumbai,
            first_name='Manager',
            last_name='User',
            employee_id='MGR-001',
            employment_status='ACTIVE',
            joining_date=self.today
        )

        # Employee 1 (Mumbai)
        self.emp1_user = User.objects.create_user(
            email='emp1@attendancecorp.com',
            password='Password123!',
            first_name='Rahul',
            last_name='Sharma'
        )
        BusinessMembership.objects.create(
            business=self.biz,
            user=self.emp1_user,
            role=BusinessRole.STAFF
        )
        self.emp1 = Employee.objects.create(
            business=self.biz,
            user=self.emp1_user,
            branch=self.centre_mumbai,
            first_name='Rahul',
            last_name='Sharma',
            employee_id='EMP-001',
            employment_status='ACTIVE',
            joining_date=self.today
        )

        # Employee 2 (Delhi)
        self.emp2_user = User.objects.create_user(
            email='emp2@attendancecorp.com',
            password='Password123!',
            first_name='Pooja',
            last_name='Verma'
        )
        BusinessMembership.objects.create(
            business=self.biz,
            user=self.emp2_user,
            role=BusinessRole.STAFF
        )
        self.emp2 = Employee.objects.create(
            business=self.biz,
            user=self.emp2_user,
            branch=self.centre_delhi,
            first_name='Pooja',
            last_name='Verma',
            employee_id='EMP-002',
            employment_status='ACTIVE',
            joining_date=self.today
        )

    def test_01_normal_punch_flow(self):
        """Test standard punch-in and punch-out flow without GPS requirement."""
        self.client.force_authenticate(user=self.emp1_user)

        # Check-in
        res = self.client.post('/api/v1/attendance/check-in/', {
            'attendance_method': 'NORMAL',
            'source': 'WEB'
        })
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res.data['is_checked_in'])
        self.assertEqual(res.data['day_status'], 'PRESENT')

        # Duplicate check-in rejected
        res_dup = self.client.post('/api/v1/attendance/check-in/', {
            'attendance_method': 'NORMAL'
        })
        self.assertEqual(res_dup.status_code, status.HTTP_400_BAD_REQUEST)

        # Check-out
        res_out = self.client.post('/api/v1/attendance/check-out/', {
            'attendance_method': 'NORMAL',
            'source': 'WEB'
        })
        self.assertEqual(res_out.status_code, status.HTTP_200_OK)
        self.assertFalse(res_out.data['is_checked_in'])

        # Duplicate check-out rejected
        res_out_dup = self.client.post('/api/v1/attendance/check-out/', {})
        self.assertEqual(res_out_dup.status_code, status.HTTP_400_BAD_REQUEST)

    def test_02_disabled_method_rejected(self):
        """Test that disabled attendance methods for a centre are strictly rejected."""
        # Delhi has normal punch disabled, QR only
        self.client.force_authenticate(user=self.emp2_user)

        res = self.client.post('/api/v1/attendance/check-in/', {
            'attendance_method': 'NORMAL'
        })
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('disabled', res.data['detail'].lower())

    def test_03_location_verification_enforcement(self):
        """Test geofencing validation when location verification is enabled on a centre."""
        self.client.force_authenticate(user=self.emp2_user)

        # 1. Missing coordinates rejected
        res_missing = self.client.post('/api/v1/attendance/check-in/', {
            'attendance_method': 'QR',
            'qr_code': f"OWNMANAGE:CENTRE:{self.centre_delhi.id}"
        })
        self.assertEqual(res_missing.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(res_missing.data.get('location_required'))

        # 2. Outside geofence rejected (e.g. coordinates in Mumbai instead of Delhi)
        res_outside = self.client.post('/api/v1/attendance/check-in/', {
            'attendance_method': 'QR',
            'qr_code': f"OWNMANAGE:CENTRE:{self.centre_delhi.id}",
            'latitude': 19.076000,
            'longitude': 72.877700
        })
        self.assertEqual(res_outside.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(res_outside.data.get('location_rejected'))

        # 3. Inside geofence accepted (coordinates within Delhi centre 150m)
        res_inside = self.client.post('/api/v1/attendance/check-in/', {
            'attendance_method': 'QR',
            'qr_code': f"OWNMANAGE:CENTRE:{self.centre_delhi.id}",
            'latitude': 28.704150,
            'longitude': 77.102520,
            'location_accuracy': 10.0
        })
        self.assertEqual(res_inside.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res_inside.data['location_verified'])
        self.assertEqual(res_inside.data['attendance_method'], 'QR')

    def test_04_face_recognition_flow(self):
        """Test Face Recognition attendance flow."""
        self.client.force_authenticate(user=self.emp1_user)

        # Missing face verification telemetry fails
        res_fail = self.client.post('/api/v1/attendance/check-in/', {
            'attendance_method': 'FACE',
            'face_data': {'verified': False}
        }, format='json')
        self.assertEqual(res_fail.status_code, status.HTTP_400_BAD_REQUEST)

        # Successful face verification
        res_success = self.client.post('/api/v1/attendance/check-in/', {
            'attendance_method': 'FACE',
            'face_data': {'verified': True, 'confidence': 0.99, 'liveness': True}
        }, format='json')
        self.assertEqual(res_success.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res_success.data['attendance_method'], 'FACE')

    def test_05_overtime_calculation(self):
        """Test overtime behavior when disabled (0) vs when enabled (calculated)."""
        # Create day for emp1 with 10 hours worked (600 min). Standard day = 480 min.
        # But Enterprise OT is OFF. Overtime must be 0!
        day = AttendanceDay.objects.create(
            business=self.biz,
            employee=self.emp1,
            centre=self.centre_mumbai,
            attendance_date=self.today,
            status=AttendanceStatus.PRESENT
        )
        t_in = timezone.now() - timedelta(hours=10)
        t_out = timezone.now()
        AttendanceEvent.objects.create(
            business=self.biz,
            attendance_day=day,
            employee=self.emp1,
            event_type=AttendanceEventType.CHECK_IN,
            event_time=t_in
        )
        AttendanceEvent.objects.create(
            business=self.biz,
            attendance_day=day,
            employee=self.emp1,
            event_type=AttendanceEventType.CHECK_OUT,
            event_time=t_out
        )

        calc = AttendanceCalculationService.calculate_daily_attendance(day, save=True)
        self.assertEqual(calc['overtime_seconds'], 0, "OT should be 0 when ot_enabled is False")

        # Now test with OT enabled on Centre Delhi (emp2)
        day_delhi = AttendanceDay.objects.create(
            business=self.biz,
            employee=self.emp2,
            centre=self.centre_delhi,
            attendance_date=self.today,
            status=AttendanceStatus.PRESENT
        )
        AttendanceEvent.objects.create(
            business=self.biz,
            attendance_day=day_delhi,
            employee=self.emp2,
            event_type=AttendanceEventType.CHECK_IN,
            event_time=t_in
        )
        AttendanceEvent.objects.create(
            business=self.biz,
            attendance_day=day_delhi,
            employee=self.emp2,
            event_type=AttendanceEventType.CHECK_OUT,
            event_time=t_out
        )
        delhi_policy = PolicyResolver.get_attendance_policy(centre=self.centre_delhi)['effective']
        calc_delhi = AttendanceCalculationService.calculate_daily_attendance(day_delhi, effective_policy=delhi_policy, save=True)
        self.assertGreater(calc_delhi['overtime_seconds'], 0, "OT should be > 0 when ot_enabled is True and duration exceeds threshold")

    def test_06_manager_override_and_audit(self):
        """Test manager manual edit and override preserves original values and writes audit trail."""
        day = AttendanceDay.objects.create(
            business=self.biz,
            employee=self.emp1,
            centre=self.centre_mumbai,
            attendance_date=self.today,
            status=AttendanceStatus.LATE,
            total_work_seconds=28800
        )

        self.client.force_authenticate(user=self.admin_user)

        # Attempt override without reason fails
        res_no_reason = self.client.post(f'/api/v1/attendance/records/{day.id}/override/', {
            'status': 'PRESENT',
            'reason': ''
        })
        self.assertEqual(res_no_reason.status_code, status.HTTP_400_BAD_REQUEST)

        # Successful override with reason
        res_override = self.client.post(f'/api/v1/attendance/records/{day.id}/override/', {
            'status': 'PRESENT',
            'check_in': '09:00',
            'check_out': '18:00',
            'reason': 'Employee metro delayed due to track maintenance; approved by director'
        })
        self.assertEqual(res_override.status_code, status.HTTP_200_OK)
        self.assertTrue(res_override.data['is_overridden'])

        # Verify AttendanceDay updated and original status recorded
        day.refresh_from_db()
        self.assertTrue(day.is_overridden)
        self.assertEqual(day.status, 'PRESENT')
        self.assertEqual(day.original_status, 'LATE')
        self.assertEqual(day.overridden_by, self.admin_user)

        # Verify AuditLog created
        log = AuditLog.objects.filter(entity_type='AttendanceDay', entity_id=str(day.id)).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.action, 'ATTENDANCE_OVERRIDE')
        self.assertEqual(log.old_data['status'], 'LATE')
        self.assertEqual(log.new_data['status'], 'PRESENT')

        # Test Record Detail endpoint
        res_detail = self.client.get(f'/api/v1/attendance/records/{day.id}/')
        self.assertEqual(res_detail.status_code, status.HTTP_200_OK)
        self.assertTrue(res_detail.data['is_overridden'])
        self.assertEqual(res_detail.data['original_status'], 'LATE')
        self.assertGreaterEqual(len(res_detail.data['audit_history']), 1)

    def test_07_daily_register_not_marked(self):
        """Test daily register shows NOT_MARKED for unpunched staff without auto-creating ABSENT."""
        self.client.force_authenticate(user=self.admin_user)

        res = self.client.get(f'/api/v1/attendance/register/?date={str(self.today)}&centre_id={self.centre_mumbai.id}')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        records = res.data['records']
        emp1_rec = next((r for r in records if r['employee_id'] == str(self.emp1.id)), None)
        self.assertIsNotNone(emp1_rec)
        # Should be NOT_MARKED because today has no punches for emp1
        self.assertEqual(emp1_rec['status'], 'NOT_MARKED')
        self.assertEqual(emp1_rec['work_hours'], '—')

    def test_08_monthly_register_matrix(self):
        """Test monthly register matrix endpoint."""
        self.client.force_authenticate(user=self.admin_user)

        res = self.client.get(f'/api/v1/attendance/monthly-register/?year={self.today.year}&month={self.today.month}&centre_id={self.centre_mumbai.id}')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn('employees', res.data)
        self.assertIn('summary', res.data)
        self.assertIn('days_header', res.data)

    def test_09_qr_token_generation(self):
        """Test centre dynamic QR token generation."""
        self.client.force_authenticate(user=self.admin_user)

        res = self.client.get(f'/api/v1/attendance/qr/centre-token/?centre_id={self.centre_mumbai.id}')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn('OWNMANAGE:CENTRE:', res.data['qr_code'])
        self.assertEqual(res.data['centre_id'], str(self.centre_mumbai.id))
