from datetime import date, datetime, time, timedelta
from decimal import Decimal
from django.test import TestCase
from django.db import IntegrityError, transaction
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient
from rest_framework import status
from rest_framework.exceptions import ValidationError as DRFValidationError

from apps.accounts.models import User
from apps.organization.models import (
    Business, BusinessMembership, BusinessRole,
    Branch, Department, Employee
)
from apps.attendance.models import (
    AttendancePolicy, AttendancePolicyOverride, AttendanceDay,
    AttendanceEvent, AttendanceStatus, AttendanceMethod, AttendanceEventType,
    EmployeeWorkingHour
)
from apps.attendance.services.employee_working_hours_service import EmployeeWorkingHoursService
from apps.attendance.services.attendance_calculation_service import AttendanceCalculationService
from apps.organization.services.policy_resolver import PolicyResolver
from apps.organization.serializers import EmployeeCreateSerializer
from apps.payroll.models import PayrollRun, PayrollLineItem, SalaryStructure


class EmployeeWorkingHoursSystemTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.today = date.today()

        # Business
        self.biz = Business.objects.create(
            name='Alpha Corp',
            timezone='Asia/Kolkata',
            currency='INR'
        )

        # Enterprise Attendance Policy: Mon-Fri 09:00-18:00, Sat-Sun Off (weekly_off_days: [5, 6])
        self.ent_policy = AttendancePolicy.objects.create(
            business=self.biz,
            office_start=time(9, 0),
            office_end=time(18, 0),
            break_start=time(13, 0),
            break_end=time(14, 0),
            weekly_off_days=[5, 6],
            grace_period_minutes=15,
            minimum_present_minutes=480,
            minimum_half_day_minutes=240,
            ot_enabled=False
        )

        # Centre A: inherits Enterprise default
        self.centre_a = Branch.objects.create(
            business=self.biz,
            name='Centre A (North)',
            code='CEN-A',
            status='ACTIVE'
        )

        # Centre B: has Centre Override (Mon-Sat 10:00-19:00, Sun Off)
        self.centre_b = Branch.objects.create(
            business=self.biz,
            name='Centre B (South)',
            code='CEN-B',
            status='ACTIVE'
        )
        self.override_b = AttendancePolicyOverride.objects.create(
            centre=self.centre_b,
            office_start=time(10, 0),
            office_end=time(19, 0),
            break_start=time(13, 30),
            break_end=time(14, 30),
            weekly_off_days=[6]  # Only Sunday is off
        )

        # Users & Memberships
        self.superadmin = User.objects.create_superuser(email='super@test.com', password='pw')
        self.biz_admin = User.objects.create(email='admin@test.com', first_name='Admin')
        BusinessMembership.objects.create(business=self.biz, user=self.biz_admin, role=BusinessRole.BUSINESS_ADMIN)

        self.mgr_a_user = User.objects.create(email='mgra@test.com', first_name='Manager A')
        BusinessMembership.objects.create(business=self.biz, user=self.mgr_a_user, role=BusinessRole.MANAGER)
        self.emp_mgr_a = Employee.objects.create(
            business=self.biz,
            user=self.mgr_a_user,
            employee_id='MGR-A',
            first_name='Manager',
            last_name='Alpha',
            branch=self.centre_a,
            joining_date=self.today
        )

        self.mgr_b_user = User.objects.create(email='mgrb@test.com', first_name='Manager B')
        BusinessMembership.objects.create(business=self.biz, user=self.mgr_b_user, role=BusinessRole.MANAGER)
        self.emp_mgr_b = Employee.objects.create(
            business=self.biz,
            user=self.mgr_b_user,
            employee_id='MGR-B',
            first_name='Manager',
            last_name='Beta',
            branch=self.centre_b,
            joining_date=self.today
        )

    # 1. Creating an employee initializes exactly seven weekday records.
    def test_employee_creation_initializes_seven_weekday_records(self):
        ser = EmployeeCreateSerializer(data={
            'first_name': 'John',
            'last_name': 'Doe',
            'email': 'john.doe@test.com',
            'designation': 'Engineer',
            'joining_date': str(self.today),
            'branch': str(self.centre_a.id)
        }, context={'business': self.biz})
        self.assertTrue(ser.is_valid(), ser.errors)
        emp = ser.save()

        records = EmployeeWorkingHour.objects.filter(employee=emp).order_by('day_of_week')
        self.assertEqual(records.count(), 7)
        weekdays = [r.day_of_week for r in records]
        self.assertEqual(weekdays, [0, 1, 2, 3, 4, 5, 6])

    # 2. Records are initialized from the assigned centre's effective policy.
    def test_records_initialized_from_assigned_centre_policy(self):
        # Create employee in Centre B (10:00 - 19:00, Sun off)
        ser = EmployeeCreateSerializer(data={
            'first_name': 'Jane',
            'last_name': 'Smith',
            'email': 'jane.smith@test.com',
            'designation': 'Support',
            'joining_date': str(self.today),
            'branch': str(self.centre_b.id)
        }, context={'business': self.biz})
        self.assertTrue(ser.is_valid(), ser.errors)
        emp = ser.save()

        records = {r.day_of_week: r for r in EmployeeWorkingHour.objects.filter(employee=emp)}
        # Monday (0) to Saturday (5) enabled with 10:00 - 19:00
        for d in range(6):
            self.assertTrue(records[d].is_enabled, f"Day {d} should be enabled")
            self.assertEqual(records[d].start_time, time(10, 0))
            self.assertEqual(records[d].end_time, time(19, 0))
            self.assertEqual(records[d].break_start, time(13, 30))
            self.assertEqual(records[d].break_end, time(14, 30))
            self.assertFalse(records[d].is_override)
            self.assertEqual(records[d].configuration_source, 'CENTRE')

        # Sunday (6) is off
        self.assertFalse(records[6].is_enabled)
        self.assertFalse(records[6].is_override)

    # 3. No duplicate employee-weekday records can be created (UniqueConstraint).
    def test_unique_constraint_prevents_duplicate_weekday_record(self):
        emp = Employee.objects.create(
            business=self.biz,
            employee_id='EMP-DUP',
            first_name='Dup',
            last_name='Test',
            branch=self.centre_a,
            joining_date=self.today
        )
        EmployeeWorkingHour.objects.create(
            employee=emp,
            day_of_week=0,
            is_enabled=True,
            start_time=time(9, 0),
            end_time=time(18, 0)
        )
        with self.assertRaises(IntegrityError):
            EmployeeWorkingHour.objects.create(
                employee=emp,
                day_of_week=0,
                is_enabled=True,
                start_time=time(10, 0),
                end_time=time(19, 0)
            )

    # 4. All seven days remain represented, including disabled days.
    def test_all_seven_days_represented_including_disabled(self):
        emp = Employee.objects.create(
            business=self.biz,
            employee_id='EMP-ALL7',
            first_name='All',
            last_name='Seven',
            branch=self.centre_a,
            joining_date=self.today
        )
        schedule = EmployeeWorkingHoursService.get_or_initialize_schedule(emp)
        self.assertEqual(len(schedule['days']), 7)
        enabled_count = sum(1 for d in schedule['days'] if d['is_enabled'])
        disabled_count = sum(1 for d in schedule['days'] if not d['is_enabled'])
        self.assertEqual(enabled_count, 5) # Mon-Fri
        self.assertEqual(disabled_count, 2) # Sat, Sun

    # 5. An employee override affects only that employee.
    def test_employee_override_affects_only_target_employee(self):
        emp1 = Employee.objects.create(
            business=self.biz,
            employee_id='EMP-1',
            first_name='Alice',
            branch=self.centre_a,
            joining_date=self.today
        )
        emp2 = Employee.objects.create(
            business=self.biz,
            employee_id='EMP-2',
            first_name='Bob',
            branch=self.centre_a,
            joining_date=self.today
        )
        EmployeeWorkingHoursService.initialize_employee_schedule(emp1)
        EmployeeWorkingHoursService.initialize_employee_schedule(emp2)

        # Override emp1: Monday start at 08:00
        days_payload = []
        for d in range(7):
            days_payload.append({
                'day_of_week': d,
                'is_enabled': d < 5,
                'start_time': '08:00' if d == 0 else '09:00',
                'end_time': '17:00' if d == 0 else '18:00',
                'break_start': '12:00',
                'break_end': '13:00'
            })
        EmployeeWorkingHoursService.update_employee_schedule(emp1, days_payload, user=self.biz_admin)

        # Check emp1 has override
        emp1_mon = EmployeeWorkingHour.objects.get(employee=emp1, day_of_week=0)
        self.assertTrue(emp1_mon.is_override)
        self.assertEqual(emp1_mon.start_time, time(8, 0))

        # Check emp2 is unchanged (still inherits 09:00 from centre)
        emp2_mon = EmployeeWorkingHour.objects.get(employee=emp2, day_of_week=0)
        self.assertFalse(emp2_mon.is_override)
        self.assertEqual(emp2_mon.start_time, time(9, 0))

    # 6. Disabling the override restores centre defaults.
    def test_disabling_override_restores_centre_defaults(self):
        emp = Employee.objects.create(
            business=self.biz,
            employee_id='EMP-RESET',
            first_name='Charlie',
            branch=self.centre_a,
            joining_date=self.today
        )
        EmployeeWorkingHoursService.initialize_employee_schedule(emp)

        # Apply override
        days_payload = [
            {'day_of_week': d, 'is_enabled': True, 'start_time': '07:00', 'end_time': '15:00'}
            for d in range(7)
        ]
        EmployeeWorkingHoursService.update_employee_schedule(emp, days_payload, user=self.biz_admin)
        self.assertTrue(EmployeeWorkingHour.objects.filter(employee=emp, is_override=True).exists())

        # Reset schedule to centre
        EmployeeWorkingHoursService.reset_schedule_to_centre(emp, user=self.biz_admin)
        records = {r.day_of_week: r for r in EmployeeWorkingHour.objects.filter(employee=emp)}
        self.assertFalse(records[0].is_override)
        self.assertEqual(records[0].start_time, time(9, 0)) # Centre default
        self.assertFalse(records[5].is_enabled) # Sat is off in Centre A

    # 7. Centre policy changes update inherited schedules.
    def test_centre_policy_change_syncs_inheriting_schedules(self):
        emp = Employee.objects.create(
            business=self.biz,
            employee_id='EMP-SYNC',
            first_name='David',
            branch=self.centre_a,
            joining_date=self.today
        )
        EmployeeWorkingHoursService.initialize_employee_schedule(emp)

        # Update Centre A's policy override: start time changed to 08:30
        PolicyResolver.save_centre_override(
            centre=self.centre_a,
            override_data={'office_start': '08:30', 'office_end': '17:30'},
            user=self.biz_admin
        )
        EmployeeWorkingHoursService.sync_centre_policy_change(self.centre_a)

        emp_mon = EmployeeWorkingHour.objects.get(employee=emp, day_of_week=0)
        self.assertEqual(emp_mon.start_time, time(8, 30))
        self.assertEqual(emp_mon.end_time, time(17, 30))
        self.assertFalse(emp_mon.is_override)

    # 8. Centre policy changes do not overwrite active employee overrides.
    def test_centre_policy_change_does_not_overwrite_active_overrides(self):
        emp = Employee.objects.create(
            business=self.biz,
            employee_id='EMP-PRESERVE',
            first_name='Eve',
            branch=self.centre_a,
            joining_date=self.today
        )
        EmployeeWorkingHoursService.initialize_employee_schedule(emp)

        # Eve has a custom schedule: 11:00 to 20:00
        days_payload = [
            {'day_of_week': d, 'is_enabled': d < 5, 'start_time': '11:00', 'end_time': '20:00'}
            for d in range(7)
        ]
        EmployeeWorkingHoursService.update_employee_schedule(emp, days_payload, user=self.biz_admin)

        # Centre A changes its policy to 08:00 - 17:00
        PolicyResolver.save_centre_override(
            centre=self.centre_a,
            override_data={'office_start': '08:00', 'office_end': '17:00'},
            user=self.biz_admin
        )
        EmployeeWorkingHoursService.sync_centre_policy_change(self.centre_a)

        # Eve must retain her 11:00 - 20:00 schedule
        emp_mon = EmployeeWorkingHour.objects.get(employee=emp, day_of_week=0)
        self.assertTrue(emp_mon.is_override)
        self.assertEqual(emp_mon.start_time, time(11, 0))
        self.assertEqual(emp_mon.end_time, time(20, 0))

    # 9. Disabled days are not marked absent in attendance calculations.
    def test_disabled_day_not_marked_absent_in_attendance(self):
        emp = Employee.objects.create(
            business=self.biz,
            employee_id='EMP-ATT-OFF',
            first_name='Frank',
            branch=self.centre_a,
            joining_date=self.today
        )
        EmployeeWorkingHoursService.initialize_employee_schedule(emp)

        # Pick a Saturday (weekday 5), which is disabled for Frank
        # Find next Saturday
        days_ahead = (5 - self.today.weekday()) % 7
        saturday = self.today + timedelta(days=days_ahead if days_ahead > 0 else 7)

        att_day = AttendanceDay.objects.create(
            business=self.biz,
            employee=emp,
            centre=self.centre_a,
            attendance_date=saturday,
            status=AttendanceStatus.NOT_MARKED
        )

        res = AttendanceCalculationService.calculate_daily_attendance(att_day, save=True)
        self.assertEqual(res['status'], AttendanceStatus.WEEK_OFF)
        self.assertNotEqual(res['status'], AttendanceStatus.ABSENT)

    # 10. Disabled days are excluded from normal scheduled-hour and absence-deduction calculations.
    def test_disabled_day_counted_as_weekly_off_for_payroll(self):
        emp = Employee.objects.create(
            business=self.biz,
            employee_id='EMP-PAY-OFF',
            first_name='Grace',
            branch=self.centre_a,
            joining_date=self.today
        )
        EmployeeWorkingHoursService.initialize_employee_schedule(emp)

        # On a Saturday (disabled day), calculate attendance without punches
        saturday = self.today + timedelta(days=(5 - self.today.weekday()) % 7)
        att_day = AttendanceDay.objects.create(
            business=self.biz,
            employee=emp,
            centre=self.centre_a,
            attendance_date=saturday,
            status=AttendanceStatus.NOT_MARKED
        )
        AttendanceCalculationService.calculate_daily_attendance(att_day, save=True)
        att_day.refresh_from_db()
        # It's WEEK_OFF, not ABSENT
        self.assertEqual(att_day.status, AttendanceStatus.WEEK_OFF)

    # 11. Different weekdays can have different shifts.
    def test_different_weekdays_support_different_shifts(self):
        emp = Employee.objects.create(
            business=self.biz,
            employee_id='EMP-DIFF',
            first_name='Hannah',
            branch=self.centre_a,
            joining_date=self.today
        )
        EmployeeWorkingHoursService.initialize_employee_schedule(emp)

        # Monday-Friday 09:00-18:00, Saturday half day 09:00-13:00, Sunday off
        days_payload = []
        for d in range(5):
            days_payload.append({'day_of_week': d, 'is_enabled': True, 'start_time': '09:00', 'end_time': '18:00'})
        days_payload.append({'day_of_week': 5, 'is_enabled': True, 'start_time': '09:00', 'end_time': '13:00'})
        days_payload.append({'day_of_week': 6, 'is_enabled': False})

        EmployeeWorkingHoursService.update_employee_schedule(emp, days_payload, user=self.biz_admin)

        records = {r.day_of_week: r for r in EmployeeWorkingHour.objects.filter(employee=emp)}
        self.assertEqual(records[0].start_time, time(9, 0))
        self.assertEqual(records[0].end_time, time(18, 0))
        self.assertEqual(records[5].start_time, time(9, 0))
        self.assertEqual(records[5].end_time, time(13, 0))
        self.assertFalse(records[6].is_enabled)

    # 12. Invalid enabled-day schedules are rejected.
    def test_invalid_enabled_day_schedules_rejected(self):
        emp = Employee.objects.create(
            business=self.biz,
            employee_id='EMP-INV',
            first_name='Ian',
            branch=self.centre_a,
            joining_date=self.today
        )
        EmployeeWorkingHoursService.initialize_employee_schedule(emp)

        # Case 1: End time <= Start time
        bad_payload_1 = [
            {'day_of_week': d, 'is_enabled': True, 'start_time': '18:00', 'end_time': '09:00'}
            for d in range(7)
        ]
        with self.assertRaises(DRFValidationError):
            EmployeeWorkingHoursService.update_employee_schedule(emp, bad_payload_1, user=self.biz_admin)

        # Case 2: Missing start time on enabled day
        bad_payload_2 = [
            {'day_of_week': d, 'is_enabled': True, 'start_time': None, 'end_time': '18:00'}
            for d in range(7)
        ]
        with self.assertRaises(DRFValidationError):
            EmployeeWorkingHoursService.update_employee_schedule(emp, bad_payload_2, user=self.biz_admin)

        # Case 3: Break out of shift bounds
        bad_payload_3 = [
            {
                'day_of_week': d,
                'is_enabled': True,
                'start_time': '09:00',
                'end_time': '18:00',
                'break_start': '19:00',
                'break_end': '20:00'
            }
            for d in range(7)
        ]
        with self.assertRaises(DRFValidationError):
            EmployeeWorkingHoursService.update_employee_schedule(emp, bad_payload_3, user=self.biz_admin)

    # 13. Unauthorized cross-centre edits are rejected.
    def test_unauthorized_cross_centre_edits_rejected(self):
        emp_in_b = Employee.objects.create(
            business=self.biz,
            employee_id='EMP-IN-B',
            first_name='Jack',
            branch=self.centre_b,
            joining_date=self.today
        )
        EmployeeWorkingHoursService.initialize_employee_schedule(emp_in_b)

        # Manager A attempts to edit Employee in Centre B
        self.client.force_authenticate(user=self.mgr_a_user)
        payload = {
            'days': [
                {'day_of_week': d, 'is_enabled': True, 'start_time': '09:00', 'end_time': '17:00'}
                for d in range(7)
            ]
        }
        resp = self.client.put(f'/api/v1/employees/{emp_in_b.id}/working-hours/', payload, format='json')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    # 14. Schedule updates are atomic.
    def test_schedule_updates_are_atomic(self):
        emp = Employee.objects.create(
            business=self.biz,
            employee_id='EMP-ATOMIC',
            first_name='Kevin',
            branch=self.centre_a,
            joining_date=self.today
        )
        EmployeeWorkingHoursService.initialize_employee_schedule(emp)

        # Prepare 6 valid days and 1 invalid day (Sunday end_time <= start_time)
        payload = []
        for d in range(6):
            payload.append({'day_of_week': d, 'is_enabled': True, 'start_time': '09:00', 'end_time': '18:00'})
        payload.append({'day_of_week': 6, 'is_enabled': True, 'start_time': '18:00', 'end_time': '09:00'})

        with self.assertRaises(DRFValidationError):
            EmployeeWorkingHoursService.update_employee_schedule(emp, payload, user=self.biz_admin)

        # Verify that Monday was NOT updated (remains is_override=False)
        mon = EmployeeWorkingHour.objects.get(employee=emp, day_of_week=0)
        self.assertFalse(mon.is_override)

    # 15. Historical finalized payroll remains unchanged when schedules update.
    def test_finalized_payroll_remains_unchanged_on_schedule_update(self):
        emp = Employee.objects.create(
            business=self.biz,
            employee_id='EMP-PAY-FINAL',
            first_name='Laura',
            branch=self.centre_a,
            joining_date=self.today
        )
        EmployeeWorkingHoursService.initialize_employee_schedule(emp)

        # Create a finalized payroll run and employee payroll record
        from apps.payroll.models import Payroll
        prun = PayrollRun.objects.create(
            business=self.biz,
            centre=self.centre_a,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            status='FINALIZED',
            total_net=Decimal('50000.00'),
            total_gross=Decimal('50000.00')
        )
        payroll_rec = Payroll.objects.create(
            payroll_run=prun,
            business=self.biz,
            employee=emp,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            paid_days=Decimal('30.0'),
            gross_amount=Decimal('50000.00'),
            net_amount=Decimal('50000.00'),
            status='FINALIZED'
        )

        # Now update employee's schedule
        days_payload = [
            {'day_of_week': d, 'is_enabled': d < 4, 'start_time': '10:00', 'end_time': '19:00'}
            for d in range(7)
        ]
        EmployeeWorkingHoursService.update_employee_schedule(emp, days_payload, user=self.biz_admin)

        # Verify finalized payroll record was NOT modified
        payroll_rec.refresh_from_db()
        self.assertEqual(payroll_rec.net_amount, Decimal('50000.00'))
        self.assertEqual(payroll_rec.paid_days, Decimal('30.0'))
        prun.refresh_from_db()
        self.assertEqual(prun.status, 'FINALIZED')

    # 16. AttendanceCalendarView displays WEEK_OFF (not ABSENT) for disabled/off days
    def test_attendance_calendar_shows_weekly_off_for_disabled_days(self):
        from apps.attendance.views import AttendanceCalendarView
        from rest_framework.test import APIRequestFactory, force_authenticate

        emp = Employee.objects.create(
            business=self.biz,
            employee_id='EMP-CAL-OFF',
            first_name='Mike',
            branch=self.centre_a,
            joining_date=date(2026, 1, 1)
        )
        # Sunday is disabled (is_enabled=False), Mon-Sat enabled
        days_payload = [
            {'day_of_week': d, 'is_enabled': d < 6, 'start_time': '09:00', 'end_time': '18:00'}
            for d in range(7)
        ]
        EmployeeWorkingHoursService.update_employee_schedule(emp, days_payload, user=self.biz_admin)

        factory = APIRequestFactory()
        req = factory.get(f'/api/v1/attendance/calendar/?employee_id={emp.id}&year=2026&month=10')
        force_authenticate(req, user=self.biz_admin)
        resp = AttendanceCalendarView.as_view()(req)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # 2026-10-04 is Sunday. It must be WEEK_OFF, NEVER ABSENT!
        sun_oct_4 = next(d for d in resp.data['days'] if d['date'] == '2026-10-04')
        self.assertEqual(sun_oct_4['status'], 'WEEK_OFF')
        self.assertNotEqual(sun_oct_4['status'], 'ABSENT')

        # 2026-10-11 is also Sunday. It must be WEEK_OFF, not ABSENT!
        sun_oct_11 = next(d for d in resp.data['days'] if d['date'] == '2026-10-11')
        self.assertEqual(sun_oct_11['status'], 'WEEK_OFF')
        self.assertNotEqual(sun_oct_11['status'], 'ABSENT')

    # 17. EmployeeWorkingHoursView API response rules synchronize working_days_per_week and weekly_off_days
    def test_employee_working_hours_api_syncs_rules_working_days_with_custom_schedule(self):
        from apps.attendance.policy_views import EmployeeWorkingHoursView
        from rest_framework.test import APIRequestFactory, force_authenticate

        emp = Employee.objects.create(
            business=self.biz,
            employee_id='EMP-SYNC-6DAYS',
            first_name='Synchronized',
            branch=self.centre_a,
            joining_date=self.today
        )

        # 6 working days: Mon-Sat enabled, only Sunday off
        days_payload_6 = [
            {'day_of_week': d, 'is_enabled': d < 6, 'start_time': '09:00', 'end_time': '18:00'}
            for d in range(7)
        ]
        EmployeeWorkingHoursService.update_employee_schedule(emp, days_payload_6, user=self.biz_admin)

        factory = APIRequestFactory()
        req = factory.get(f'/api/v1/employees/{emp.id}/working-hours/')
        force_authenticate(req, user=self.biz_admin)
        resp = EmployeeWorkingHoursView.as_view()(req, pk=str(emp.id))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        rules = resp.data['rules']
        # Must be exactly 6 working days per week and only Sunday as weekly off!
        self.assertEqual(rules['working_days_per_week'], 6)
        self.assertEqual(rules['weekly_off_days'], ['Sunday'])
        self.assertEqual(resp.data['shift_timings']['office_start'], '09:00')
        self.assertEqual(resp.data['shift_timings']['office_end'], '18:00')

        # Now update to 5 working days (Mon-Fri enabled, Sat-Sun off)
        days_payload_5 = [
            {'day_of_week': d, 'is_enabled': d < 5, 'start_time': '10:00', 'end_time': '19:00'}
            for d in range(7)
        ]
        EmployeeWorkingHoursService.update_employee_schedule(emp, days_payload_5, user=self.biz_admin)

        req2 = factory.get(f'/api/v1/employees/{emp.id}/working-hours/')
        force_authenticate(req2, user=self.biz_admin)
        resp2 = EmployeeWorkingHoursView.as_view()(req2, pk=str(emp.id))
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)

        rules2 = resp2.data['rules']
        # Must be exactly 5 working days per week and Saturday, Sunday off!
        self.assertEqual(rules2['working_days_per_week'], 5)
        self.assertEqual(rules2['weekly_off_days'], ['Saturday', 'Sunday'])
        self.assertEqual(resp2.data['shift_timings']['office_start'], '10:00')
        self.assertEqual(resp2.data['shift_timings']['office_end'], '19:00')

