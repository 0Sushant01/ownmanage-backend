from datetime import date, time
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.exceptions import ValidationError, PermissionDenied

from apps.accounts.models import User
from apps.organization.models import Business, Branch, Employee, BusinessRole, BusinessMembership
from apps.meetings.models import (
    Meeting, MeetingParticipant, MeetingExternalGuest, MeetingStatus,
    ParticipantResponseStatus, AuditEventType
)
from apps.meetings.services.meeting_service import MeetingService
from apps.meetings.views import (
    MeetingListCreateView, MeetingDetailView, MeetingCancelView,
    MeetingRespondView, EligibleParticipantsView, MeetingConflictCheckView
)
from apps.attendance.models import AttendanceDay


class MeetingFeatureTestCase(TestCase):
    def setUp(self):
        # 1. Create Business
        self.biz = Business.objects.create(name='Acme Testing Corp', timezone='Asia/Kolkata')
        self.centre_a = Branch.objects.create(business=self.biz, name='Centre Alpha', code='C-ALP')
        self.centre_b = Branch.objects.create(business=self.biz, name='Centre Beta', code='C-BET')

        # 2. Users & Employees
        self.admin_user = User.objects.create_user(email='admin@acme.com', password='password123', first_name='Alice', last_name='Admin')
        BusinessMembership.objects.create(business=self.biz, user=self.admin_user, role=BusinessRole.BUSINESS_ADMIN)
        self.admin_emp = Employee.objects.create(business=self.biz, user=self.admin_user, first_name='Alice', last_name='Admin', branch=self.centre_a, joining_date=date(2025, 1, 1))

        self.mgr_user = User.objects.create_user(email='manager@acme.com', password='password123', first_name='Mark', last_name='Manager')
        BusinessMembership.objects.create(business=self.biz, user=self.mgr_user, role=BusinessRole.MANAGER)
        self.mgr_emp = Employee.objects.create(business=self.biz, user=self.mgr_user, first_name='Mark', last_name='Manager', branch=self.centre_a, joining_date=date(2025, 1, 1))

        self.staff1_user = User.objects.create_user(email='staff1@acme.com', password='password123', first_name='Sam', last_name='Staff1')
        BusinessMembership.objects.create(business=self.biz, user=self.staff1_user, role=BusinessRole.STAFF)
        self.staff1_emp = Employee.objects.create(business=self.biz, user=self.staff1_user, first_name='Sam', last_name='Staff1', branch=self.centre_a, joining_date=date(2025, 1, 1))

        self.staff2_user = User.objects.create_user(email='staff2@acme.com', password='password123', first_name='Sara', last_name='Staff2')
        BusinessMembership.objects.create(business=self.biz, user=self.staff2_user, role=BusinessRole.STAFF)
        self.staff2_emp = Employee.objects.create(business=self.biz, user=self.staff2_user, first_name='Sara', last_name='Staff2', branch=self.centre_b, joining_date=date(2025, 1, 1))

        self.factory = APIRequestFactory()

    def test_create_meeting_success_with_internal_and_external_guests(self):
        meeting = MeetingService.create_meeting(
            business=self.biz,
            organizer_user=self.admin_user,
            data={
                'title': 'Q4 Strategy Review',
                'description': 'Quarterly executive review and goal alignment.',
                'meeting_date': date(2026, 11, 10),
                'start_time': time(10, 0),
                'end_time': time(11, 0),
                'location_type': 'ONLINE',
                'meeting_url': 'https://meet.google.com/abc-defg-hij',
                'participant_ids': [str(self.staff1_emp.id)],
                'external_guests': [{'email': 'client@external.com', 'name': 'External Client'}]
            }
        )

        self.assertEqual(meeting.status, MeetingStatus.SCHEDULED)
        self.assertEqual(meeting.title, 'Q4 Strategy Review')
        # Organizer + 1 staff = 2 participants
        self.assertEqual(meeting.participants.count(), 2)
        org_part = meeting.participants.filter(is_organizer=True).first()
        self.assertEqual(org_part.response_status, ParticipantResponseStatus.ACCEPTED)

        staff_part = meeting.participants.filter(is_organizer=False).first()
        self.assertEqual(staff_part.response_status, ParticipantResponseStatus.PENDING)

        # 1 external guest
        self.assertEqual(meeting.external_guests.count(), 1)
        ext_guest = meeting.external_guests.first()
        self.assertEqual(ext_guest.email, 'client@external.com')
        self.assertIsNotNone(ext_guest.invitation_token)

        # External guest did NOT create a user or employee
        self.assertFalse(User.objects.filter(email='client@external.com').exists())
        self.assertFalse(Employee.objects.filter(email='client@external.com').exists())

    def test_invalid_time_boundaries_rejected(self):
        with self.assertRaises(ValidationError):
            MeetingService.create_meeting(
                business=self.biz,
                organizer_user=self.admin_user,
                data={
                    'title': 'Invalid Meeting',
                    'meeting_date': date(2026, 11, 10),
                    'start_time': time(11, 0),
                    'end_time': time(10, 0),  # End time before start time
                }
            )

    def test_soft_conflict_detection_warns_without_blocking(self):
        # Create first meeting
        MeetingService.create_meeting(
            business=self.biz,
            organizer_user=self.admin_user,
            data={
                'title': 'Morning Standup',
                'meeting_date': date(2026, 11, 15),
                'start_time': time(10, 0),
                'end_time': time(11, 0),
                'participant_ids': [str(self.staff1_emp.id)]
            }
        )

        # Check conflicts for overlapping slot 10:30 - 11:30
        conflicts = MeetingService.check_conflicts(
            business=self.biz,
            meeting_date=date(2026, 11, 15),
            start_time=time(10, 30),
            end_time=time(11, 30),
            organizer_user=self.mgr_user,
            employee_ids=[str(self.staff1_emp.id)],
            caller_user=self.mgr_user
        )

        self.assertTrue(len(conflicts) > 0)
        emp_conflict = next((c for c in conflicts if c.get('employee_id') == str(self.staff1_emp.id)), None)
        self.assertIsNotNone(emp_conflict)
        self.assertIn('already scheduled', emp_conflict['warning'])

        # Proceeding despite warning still creates the meeting successfully
        m2 = MeetingService.create_meeting(
            business=self.biz,
            organizer_user=self.mgr_user,
            data={
                'title': 'Design Critique',
                'meeting_date': date(2026, 11, 15),
                'start_time': time(10, 30),
                'end_time': time(11, 30),
                'participant_ids': [str(self.staff1_emp.id)]
            }
        )
        self.assertEqual(m2.status, MeetingStatus.SCHEDULED)

    def test_participant_rsvp_independent_and_declining_does_not_cancel(self):
        meeting = MeetingService.create_meeting(
            business=self.biz,
            organizer_user=self.admin_user,
            data={
                'title': 'Team Sync',
                'meeting_date': date(2026, 11, 20),
                'start_time': time(14, 0),
                'end_time': time(15, 0),
                'participant_ids': [str(self.staff1_emp.id)]
            }
        )

        # Staff responds with DECLINED
        part = MeetingService.respond_to_meeting(
            meeting=meeting,
            user=self.staff1_user,
            response_status='DECLINED',
            note='Out of office'
        )

        self.assertEqual(part.response_status, ParticipantResponseStatus.DECLINED)
        # Meeting status remains SCHEDULED
        meeting.refresh_from_db()
        self.assertEqual(meeting.status, MeetingStatus.SCHEDULED)

    def test_cancellation_marks_cancelled_with_reason(self):
        meeting = MeetingService.create_meeting(
            business=self.biz,
            organizer_user=self.admin_user,
            data={
                'title': 'Project Kickoff',
                'meeting_date': date(2026, 11, 22),
                'start_time': time(9, 0),
                'end_time': time(10, 0),
                'participant_ids': [str(self.staff1_emp.id)]
            }
        )

        cancelled = MeetingService.cancel_meeting(
            meeting=meeting,
            user=self.admin_user,
            reason='Client postponed release'
        )

        self.assertEqual(cancelled.status, MeetingStatus.CANCELLED)
        self.assertEqual(cancelled.cancellation_reason, 'Client postponed release')
        self.assertIsNotNone(cancelled.cancelled_at)

    def test_eligible_participants_respects_manager_centre_boundary(self):
        # Manager is in Centre Alpha, staff1 is Centre Alpha, staff2 is Centre Beta
        results = MeetingService.get_eligible_participants(
            business=self.biz,
            caller_user=self.mgr_user
        )

        ids = [r['id'] for r in results]
        self.assertIn(str(self.staff1_emp.id), ids)
        self.assertNotIn(str(self.staff2_emp.id), ids)  # Staff 2 belongs to Centre Beta

    def test_meetings_never_alter_attendance_records(self):
        initial_attendance_count = AttendanceDay.objects.count()

        MeetingService.create_meeting(
            business=self.biz,
            organizer_user=self.admin_user,
            data={
                'title': 'All-Hands',
                'meeting_date': date(2026, 11, 25),
                'start_time': time(10, 0),
                'end_time': time(12, 0),
                'participant_ids': [str(self.staff1_emp.id)]
            }
        )

        # AttendanceDay count remains exactly the same
        self.assertEqual(AttendanceDay.objects.count(), initial_attendance_count)
