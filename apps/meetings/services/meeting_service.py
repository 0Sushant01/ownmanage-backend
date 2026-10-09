import re
from datetime import datetime, time, timedelta
from typing import List, Dict, Any, Optional
from django.db import models, transaction
from django.utils import timezone
from django.conf import settings
from rest_framework.exceptions import ValidationError, PermissionDenied, NotFound

from apps.core.models import Notification, NotificationType
from apps.core.services.audit_service import AuditService
from apps.organization.models import Business, Branch, Employee, BusinessRole, BusinessMembership
from apps.organization.services.permission_service import PermissionService
from apps.meetings.models import (
    Meeting, MeetingParticipant, MeetingExternalGuest, MeetingAuditEvent,
    MeetingNotificationOutbox, MeetingStatus, MeetingLocationType,
    ParticipantResponseStatus, AuditEventType
)


class MeetingService:

    @classmethod
    def check_conflicts(
        cls,
        business: Business,
        meeting_date,
        start_time,
        end_time,
        organizer_user,
        employee_ids: List[str],
        exclude_meeting_id: Optional[str] = None,
        caller_user = None
    ) -> List[Dict[str, Any]]:
        """
        Soft conflict detection engine.
        Returns a list of overlapping meeting warnings for organizer and participants.
        Does NOT block creation; provides privacy-safe warnings.
        """
        conflicts = []
        if not meeting_date or not start_time or not end_time:
            return conflicts

        # Base query for overlapping scheduled meetings on that date
        qs = Meeting.objects.filter(
            business=business,
            meeting_date=meeting_date,
            status=MeetingStatus.SCHEDULED,
            start_time__lt=end_time,
            end_time__gt=start_time
        ).select_related('organizer', 'branch')

        if exclude_meeting_id:
            qs = qs.exclude(id=exclude_meeting_id)

        # 1. Check organizer conflict
        org_meetings = qs.filter(
            models.Q(organizer=organizer_user) |
            models.Q(participants__employee__user=organizer_user)
        ).distinct()

        for m in org_meetings:
            conflicts.append({
                'entity_type': 'ORGANIZER',
                'user_id': str(organizer_user.id),
                'name': organizer_user.get_full_name(),
                'conflict_meeting_id': str(m.id),
                'meeting_title': m.title,
                'start_time': m.start_time.strftime('%H:%M'),
                'end_time': m.end_time.strftime('%H:%M'),
                'warning': f"Organizer {organizer_user.get_full_name()} has an overlapping meeting: '{m.title}' ({m.start_time.strftime('%H:%M')} - {m.end_time.strftime('%H:%M')})."
            })

        # 2. Check participants conflicts
        if employee_ids:
            part_meetings = qs.filter(
                participants__employee_id__in=employee_ids
            ).prefetch_related('participants__employee').distinct()

            for m in part_meetings:
                for p in m.participants.filter(employee_id__in=employee_ids):
                    emp = p.employee
                    # Check caller authorization to view meeting details
                    can_view_title = (
                        caller_user and (
                            caller_user.is_superuser or
                            m.organizer_id == caller_user.id or
                            m.participants.filter(employee__user=caller_user).exists()
                        )
                    )
                    title_display = m.title if can_view_title else "Scheduled Meeting"
                    conflicts.append({
                        'entity_type': 'PARTICIPANT',
                        'employee_id': str(emp.id),
                        'name': emp.full_name,
                        'conflict_meeting_id': str(m.id),
                        'meeting_title': title_display,
                        'start_time': m.start_time.strftime('%H:%M'),
                        'end_time': m.end_time.strftime('%H:%M'),
                        'warning': f"{emp.full_name} is already scheduled for '{title_display}' ({m.start_time.strftime('%H:%M')} - {m.end_time.strftime('%H:%M')})."
                    })

        return conflicts

    @classmethod
    def get_eligible_participants(
        cls,
        business: Business,
        caller_user,
        search_query: str = '',
        centre_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Returns employees eligible to be invited, strictly respecting multi-tenant boundaries
        and manager centre/direct-report access.
        """
        qs = Employee.objects.filter(
            business=business,
            employment_status='ACTIVE'
        ).select_related('branch', 'department', 'user')

        # Role & Scope enforcement
        membership = BusinessMembership.objects.filter(
            business=business,
            user=caller_user,
            is_active=True
        ).first()

        is_admin = caller_user.is_superuser or (membership and membership.role == BusinessRole.BUSINESS_ADMIN)
        is_manager = membership and membership.role == BusinessRole.MANAGER

        if not is_admin:
            if is_manager:
                manager_emp = getattr(caller_user, 'employee_profiles', None)
                manager_emp_obj = manager_emp.filter(business=business).first() if manager_emp else None
                if manager_emp_obj:
                    # Permitted: same branch OR direct reports OR self
                    q_filter = models.Q(id=manager_emp_obj.id) | models.Q(manager_id=manager_emp_obj.id)
                    if manager_emp_obj.branch_id:
                        q_filter |= models.Q(branch_id=manager_emp_obj.branch_id)
                    qs = qs.filter(q_filter)
                else:
                    return []
            else:
                # Regular staff: only return self unless granted general search
                qs = qs.filter(user=caller_user)

        if centre_id:
            qs = qs.filter(branch_id=centre_id)

        if search_query:
            sq = search_query.strip()
            qs = qs.filter(
                models.Q(first_name__icontains=sq) |
                models.Q(last_name__icontains=sq) |
                models.Q(employee_id__icontains=sq) |
                models.Q(email__icontains=sq)
            )

        results = []
        for emp in qs.order_by('first_name', 'last_name')[:30]:
            results.append({
                'id': str(emp.id),
                'employee_id': emp.employee_id or '—',
                'full_name': emp.full_name,
                'email': emp.email,
                'designation': emp.designation or '—',
                'branch_id': str(emp.branch_id) if emp.branch_id else None,
                'branch_name': emp.branch.name if emp.branch else 'Unassigned',
                'department_name': emp.department.name if emp.department else 'General',
                'user_id': str(emp.user_id) if emp.user_id else None
            })

        return results

    @classmethod
    def validate_organizer_permissions(cls, business: Business, user) -> None:
        """
        Validates whether user has permission to create meetings.
        """
        if user.is_superuser:
            return

        has_perm = PermissionService.has_permission(
            user=user,
            permission_key='meetings.create',
            business=business
        )
        if not has_perm:
            raise PermissionDenied("You do not have permission to schedule meetings.")

    @classmethod
    def create_meeting(
        cls,
        business: Business,
        organizer_user,
        data: Dict[str, Any]
    ) -> Meeting:
        """
        Creates a meeting, registers internal and external participants,
        records audit events, and dispatches in-app notifications.
        """
        cls.validate_organizer_permissions(business, organizer_user)

        # 1. Validate Time Boundaries
        meeting_date = data.get('meeting_date')
        start_time = data.get('start_time')
        end_time = data.get('end_time')

        if not meeting_date or not start_time or not end_time:
            raise ValidationError("Meeting date, start time, and end time are required.")

        if end_time <= start_time:
            raise ValidationError("Meeting end time must be after start time.")

        # 2. Check external invite permissions if external guests included
        external_guests = data.get('external_guests', []) or []
        if external_guests:
            has_ext_perm = PermissionService.has_permission(
                user=organizer_user,
                permission_key='meetings.invite_external',
                business=business
            )
            if not (organizer_user.is_superuser or has_ext_perm):
                raise PermissionDenied("You do not have permission to invite external guests.")

        # 3. Resolve organizer employee profile and branch
        org_emp = Employee.objects.filter(business=business, user=organizer_user).first()
        branch_id = data.get('branch_id')
        branch = None
        if branch_id:
            branch = Branch.objects.filter(business=business, id=branch_id).first()
        elif org_emp and org_emp.branch_id:
            branch = org_emp.branch

        # 4. Validate internal participants
        participant_emp_ids = data.get('participant_ids', []) or []
        participant_employees = []
        if participant_emp_ids:
            participant_employees = list(Employee.objects.filter(
                business=business,
                id__in=participant_emp_ids,
                employment_status='ACTIVE'
            ).select_related('user'))

            if len(participant_employees) != len(set(participant_emp_ids)):
                raise ValidationError("One or more selected participants are invalid or inactive.")

        with transaction.atomic():
            meeting = Meeting.objects.create(
                business=business,
                branch=branch,
                title=data.get('title', '').strip(),
                description=data.get('description', '').strip(),
                meeting_date=meeting_date,
                start_time=start_time,
                end_time=end_time,
                timezone=data.get('timezone', 'Asia/Kolkata'),
                location_type=data.get('location_type', MeetingLocationType.ONLINE),
                location_details=data.get('location_details', '').strip(),
                meeting_url=data.get('meeting_url') or None,
                organizer=organizer_user,
                organizer_employee=org_emp,
                status=MeetingStatus.SCHEDULED
            )

            # Add Organizer as participant (ACCEPTED)
            if org_emp:
                MeetingParticipant.objects.create(
                    meeting=meeting,
                    employee=org_emp,
                    response_status=ParticipantResponseStatus.ACCEPTED,
                    responded_at=timezone.now(),
                    is_organizer=True,
                    is_optional=False
                )

            # Add Invited Internal Staff (PENDING)
            created_parts = []
            for emp in participant_employees:
                if org_emp and emp.id == org_emp.id:
                    continue  # Organizer already added
                p = MeetingParticipant.objects.create(
                    meeting=meeting,
                    employee=emp,
                    response_status=ParticipantResponseStatus.PENDING,
                    is_organizer=False,
                    is_optional=False
                )
                created_parts.append(p)

            # Add External Guests (Stored separately, no User/Employee account)
            email_regex = re.compile(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$')
            seen_emails = set()
            for guest in external_guests:
                guest_email = guest.get('email', '').strip().lower()
                guest_name = guest.get('name', '').strip()
                if not guest_email or not email_regex.match(guest_email):
                    raise ValidationError(f"Invalid external guest email address: '{guest_email}'.")
                if guest_email in seen_emails:
                    continue
                seen_emails.add(guest_email)

                MeetingExternalGuest.objects.create(
                    meeting=meeting,
                    email=guest_email,
                    name=guest_name,
                    response_status=ParticipantResponseStatus.PENDING
                )

                # Outbox record for future email phase
                MeetingNotificationOutbox.objects.create(
                    meeting=meeting,
                    recipient_email=guest_email,
                    recipient_type='EXTERNAL_GUEST',
                    event_type='INVITATION',
                    payload={'meeting_title': meeting.title, 'date': str(meeting.meeting_date)}
                )

            # Record Audit Trail
            MeetingAuditEvent.objects.create(
                meeting=meeting,
                actor=organizer_user,
                event_type=AuditEventType.CREATED,
                summary=f"Meeting scheduled with {len(created_parts)} staff and {len(seen_emails)} external guests.",
                diff_data={
                    'title': meeting.title,
                    'date': str(meeting.meeting_date),
                    'start_time': str(meeting.start_time),
                    'end_time': str(meeting.end_time),
                }
            )

            AuditService.log(
                user_or_request=organizer_user,
                action='CREATE_MEETING',
                entity_type='Meeting',
                entity_id=str(meeting.id),
                new_data={'title': meeting.title, 'date': str(meeting.meeting_date)},
                business=business,
                reason="Scheduled new meeting"
            )

            # Dispatch In-App Notifications to Internal Invitees
            for part in created_parts:
                if part.employee.user:
                    Notification.objects.create(
                        user=part.employee.user,
                        business=business,
                        title="Meeting Invitation",
                        message=f"You are invited to '{meeting.title}' on {meeting.meeting_date} at {meeting.start_time.strftime('%H:%M')}.",
                        notification_type=NotificationType.MEETING_INVITATION
                    )

            return meeting

    @classmethod
    def update_meeting(
        cls,
        meeting: Meeting,
        user,
        data: Dict[str, Any]
    ) -> Meeting:
        """
        Updates meeting details or reschedules date/time.
        Dispatches in-app update notifications to participants.
        """
        # Permission check: must be organizer or have meetings.edit_any
        is_organizer = meeting.organizer_id == user.id
        has_edit_any = PermissionService.has_permission(user, 'meetings.edit_any', meeting.business)
        if not (user.is_superuser or is_organizer or has_edit_any):
            raise PermissionDenied("You do not have permission to edit this meeting.")

        if meeting.status == MeetingStatus.CANCELLED:
            raise ValidationError("Cannot modify a cancelled meeting.")

        diff = {}
        is_rescheduled = False

        if 'title' in data and data['title']:
            diff['title'] = (meeting.title, data['title'])
            meeting.title = data['title'].strip()

        if 'description' in data:
            meeting.description = (data['description'] or '').strip()

        if 'location_type' in data:
            meeting.location_type = data['location_type']
        if 'location_details' in data:
            meeting.location_details = (data['location_details'] or '').strip()
        if 'meeting_url' in data:
            meeting.meeting_url = data['meeting_url'] or None

        new_date = data.get('meeting_date')
        new_start = data.get('start_time')
        new_end = data.get('end_time')

        if new_date or new_start or new_end:
            target_date = new_date or meeting.meeting_date
            target_start = new_start or meeting.start_time
            target_end = new_end or meeting.end_time

            if target_end <= target_start:
                raise ValidationError("Meeting end time must be after start time.")

            if (target_date != meeting.meeting_date or
                target_start != meeting.start_time or
                target_end != meeting.end_time):
                is_rescheduled = True
                diff['time'] = (
                    f"{meeting.meeting_date} {meeting.start_time}-{meeting.end_time}",
                    f"{target_date} {target_start}-{target_end}"
                )
                meeting.meeting_date = target_date
                meeting.start_time = target_start
                meeting.end_time = target_end

        meeting.save()

        # Audit Event
        event_type = AuditEventType.RESCHEDULED if is_rescheduled else AuditEventType.UPDATED
        summary = "Meeting rescheduled." if is_rescheduled else "Meeting details updated."
        MeetingAuditEvent.objects.create(
            meeting=meeting,
            actor=user,
            event_type=event_type,
            summary=summary,
            diff_data=diff
        )

        # In-App Notifications to internal participants
        for part in meeting.participants.select_related('employee__user'):
            if part.employee.user and part.employee.user.id != user.id:
                notif_msg = (
                    f"'{meeting.title}' has been rescheduled to {meeting.meeting_date} at {meeting.start_time.strftime('%H:%M')}."
                    if is_rescheduled else
                    f"Details for '{meeting.title}' on {meeting.meeting_date} have been updated."
                )
                Notification.objects.create(
                    user=part.employee.user,
                    business=meeting.business,
                    title="Meeting Updated",
                    message=notif_msg,
                    notification_type=NotificationType.MEETING_UPDATE
                )

        return meeting

    @classmethod
    def cancel_meeting(
        cls,
        meeting: Meeting,
        user,
        reason: str = ''
    ) -> Meeting:
        """
        Cancels meeting with mandatory audit reason and alerts participants.
        """
        is_organizer = meeting.organizer_id == user.id
        has_cancel_any = PermissionService.has_permission(user, 'meetings.cancel_any', meeting.business)
        if not (user.is_superuser or is_organizer or has_cancel_any):
            raise PermissionDenied("You do not have permission to cancel this meeting.")

        if meeting.status == MeetingStatus.CANCELLED:
            return meeting

        meeting.status = MeetingStatus.CANCELLED
        meeting.cancellation_reason = reason.strip()
        meeting.cancelled_at = timezone.now()
        meeting.cancelled_by = user
        meeting.save()

        # Audit Event
        MeetingAuditEvent.objects.create(
            meeting=meeting,
            actor=user,
            event_type=AuditEventType.CANCELLED,
            summary=f"Meeting cancelled. Reason: {reason or 'No reason provided'}",
            diff_data={'reason': reason}
        )

        AuditService.log(
            user_or_request=user,
            action='CANCEL_MEETING',
            entity_type='Meeting',
            entity_id=str(meeting.id),
            business=meeting.business,
            reason=reason or "Meeting cancelled"
        )

        # Notify participants
        for part in meeting.participants.select_related('employee__user'):
            if part.employee.user and part.employee.user.id != user.id:
                Notification.objects.create(
                    user=part.employee.user,
                    business=meeting.business,
                    title="Meeting Cancelled",
                    message=f"'{meeting.title}' scheduled for {meeting.meeting_date} has been cancelled. Reason: {reason or 'Cancelled by organizer.'}",
                    notification_type=NotificationType.MEETING_CANCELLED
                )

        return meeting

    @classmethod
    def respond_to_meeting(
        cls,
        meeting: Meeting,
        user,
        response_status: str,
        note: str = ''
    ) -> MeetingParticipant:
        """
        Allows an internal employee to accept, decline, or mark tentative their RSVP.
        Declining does NOT cancel the meeting for others.
        """
        valid_statuses = [
            ParticipantResponseStatus.ACCEPTED,
            ParticipantResponseStatus.DECLINED,
            ParticipantResponseStatus.TENTATIVE
        ]
        if response_status not in valid_statuses:
            raise ValidationError(f"Invalid response status. Must be one of: {valid_statuses}.")

        # Find employee profile for user
        emp = Employee.objects.filter(business=meeting.business, user=user).first()
        if not emp:
            raise PermissionDenied("User does not have an employee profile in this business.")

        part = MeetingParticipant.objects.filter(meeting=meeting, employee=emp).first()
        if not part:
            raise PermissionDenied("You are not invited to this meeting.")

        part.response_status = response_status
        part.response_note = note.strip()
        part.responded_at = timezone.now()
        part.save()

        # Audit event
        MeetingAuditEvent.objects.create(
            meeting=meeting,
            actor=user,
            event_type=AuditEventType.RESPONSE_CHANGED,
            summary=f"{emp.full_name} changed RSVP to {response_status}.",
            diff_data={'employee_id': str(emp.id), 'status': response_status}
        )

        return part
