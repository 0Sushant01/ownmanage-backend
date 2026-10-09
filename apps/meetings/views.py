from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, NotFound, ValidationError
from django.db import models

from apps.core.permissions import get_user_context
from apps.organization.models import BusinessRole
from apps.organization.services.permission_service import PermissionService
from apps.meetings.models import Meeting, MeetingStatus
from apps.meetings.serializers import (
    MeetingListSerializer, MeetingDetailSerializer,
    MeetingCreateUpdateSerializer, MeetingConflictCheckSerializer
)
from apps.meetings.services.meeting_service import MeetingService


class MeetingBaseView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get_business(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if not biz:
            raise ValidationError("Tenant business context could not be determined.")
        return biz, ctx


class MeetingListCreateView(MeetingBaseView):
    """
    List and schedule meetings. Enforces tenant boundaries and role visibility.
    """
    def get(self, request):
        biz, ctx = self.get_business(request)
        user = request.user
        role = ctx['role']

        qs = Meeting.objects.filter(business=biz).select_related(
            'branch', 'organizer', 'organizer_employee'
        ).prefetch_related('participants__employee', 'external_guests')

        # Filter: Scope
        scope = request.query_params.get('scope', 'my_meetings').lower()

        # Check permissions for seeing all meetings
        can_view_all_ent = user.is_superuser or (role == BusinessRole.BUSINESS_ADMIN) or PermissionService.has_permission(user, 'meetings.view_all_enterprise', biz)
        can_view_all_centre = can_view_all_ent or PermissionService.has_permission(user, 'meetings.view_all_centre', biz)

        if scope == 'organized':
            qs = qs.filter(organizer=user)
        elif scope == 'all':
            if can_view_all_ent:
                pass  # View all
            elif can_view_all_centre:
                # Scoped to manager centre
                manager_emp = getattr(user, 'employee_profiles', None)
                m_obj = manager_emp.filter(business=biz).first() if manager_emp else None
                if m_obj and m_obj.branch_id:
                    qs = qs.filter(branch_id=m_obj.branch_id)
                else:
                    qs = qs.filter(models.Q(organizer=user) | models.Q(participants__employee__user=user))
            else:
                qs = qs.filter(models.Q(organizer=user) | models.Q(participants__employee__user=user))
        else:
            # Default 'my_meetings': meetings user organized OR is invited to
            qs = qs.filter(
                models.Q(organizer=user) |
                models.Q(participants__employee__user=user)
            ).distinct()

        # Date Filters
        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
        if date_from:
            qs = qs.filter(meeting_date__gte=date_from)
        if date_to:
            qs = qs.filter(meeting_date__lte=date_to)

        # Status Filter
        status_param = request.query_params.get('status')
        if status_param and status_param.upper() in dict(MeetingStatus.choices):
            qs = qs.filter(status=status_param.upper())

        # Centre Filter
        centre_id = request.query_params.get('centre_id')
        if centre_id:
            qs = qs.filter(branch_id=centre_id)

        # Search Query
        search = request.query_params.get('search')
        if search:
            s = search.strip()
            qs = qs.filter(
                models.Q(title__icontains=s) |
                models.Q(description__icontains=s) |
                models.Q(location_details__icontains=s)
            )

        serializer = MeetingListSerializer(qs.order_by('meeting_date', 'start_time'), many=True, context={'request': request})
        return Response(serializer.data)

    def post(self, request):
        biz, ctx = self.get_business(request)
        serializer = MeetingCreateUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        meeting = MeetingService.create_meeting(
            business=biz,
            organizer_user=request.user,
            data=serializer.validated_data
        )

        detail_serializer = MeetingDetailSerializer(meeting, context={'request': request})
        return Response(detail_serializer.data, status=status.HTTP_201_CREATED)


class MeetingDetailView(MeetingBaseView):
    """
    Retrieve or update a specific meeting.
    """
    def get_meeting(self, request, pk):
        biz, _ = self.get_business(request)
        meeting = Meeting.objects.filter(
            id=pk,
            business=biz
        ).select_related(
            'branch', 'organizer', 'organizer_employee'
        ).prefetch_related(
            'participants__employee__branch',
            'external_guests',
            'audit_events__actor'
        ).first()

        if not meeting:
            raise NotFound("Meeting not found.")

        # Access verification: must be admin, organizer, invited participant, or centre manager
        user = request.user
        is_participant = meeting.participants.filter(employee__user=user).exists()
        is_organizer = meeting.organizer_id == user.id
        can_view_all = user.is_superuser or PermissionService.has_permission(user, 'meetings.view_all_enterprise', biz)

        if not (is_participant or is_organizer or can_view_all):
            # Check centre manager
            can_view_centre = PermissionService.has_permission(user, 'meetings.view_all_centre', biz)
            if not can_view_centre:
                raise PermissionDenied("You do not have access to view this meeting.")

        return meeting

    def get(self, request, pk):
        meeting = self.get_meeting(request, pk)
        serializer = MeetingDetailSerializer(meeting, context={'request': request})
        return Response(serializer.data)

    def patch(self, request, pk):
        meeting = self.get_meeting(request, pk)
        serializer = MeetingCreateUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        updated_meeting = MeetingService.update_meeting(
            meeting=meeting,
            user=request.user,
            data=serializer.validated_data
        )

        return Response(MeetingDetailSerializer(updated_meeting, context={'request': request}).data)


class MeetingCancelView(MeetingBaseView):
    """
    Cancel an existing meeting with an audit reason.
    """
    def post(self, request, pk):
        biz, _ = self.get_business(request)
        meeting = Meeting.objects.filter(id=pk, business=biz).first()
        if not meeting:
            raise NotFound("Meeting not found.")

        reason = request.data.get('reason', '')
        cancelled_meeting = MeetingService.cancel_meeting(
            meeting=meeting,
            user=request.user,
            reason=reason
        )

        return Response({
            'detail': 'Meeting cancelled successfully.',
            'status': cancelled_meeting.status
        })


class MeetingRespondView(MeetingBaseView):
    """
    Submit or update internal participant RSVP response.
    """
    def post(self, request, pk):
        biz, _ = self.get_business(request)
        meeting = Meeting.objects.filter(id=pk, business=biz).first()
        if not meeting:
            raise NotFound("Meeting not found.")

        response_status_val = request.data.get('response_status')
        note = request.data.get('response_note', '')

        if not response_status_val:
            raise ValidationError("response_status is required.")

        part = MeetingService.respond_to_meeting(
            meeting=meeting,
            user=request.user,
            response_status=response_status_val.upper(),
            note=note
        )

        return Response({
            'detail': f'RSVP updated to {part.response_status}.',
            'response_status': part.response_status
        })


class EligibleParticipantsView(MeetingBaseView):
    """
    Search active employees eligible to be invited.
    Respects tenant isolation and centre/direct-report access.
    """
    def get(self, request):
        biz, _ = self.get_business(request)
        q = request.query_params.get('q', '')
        centre_id = request.query_params.get('centre_id')

        results = MeetingService.get_eligible_participants(
            business=biz,
            caller_user=request.user,
            search_query=q,
            centre_id=centre_id
        )

        return Response({'results': results})


class MeetingConflictCheckView(MeetingBaseView):
    """
    Pre-flight conflict check returning soft warnings.
    """
    def post(self, request):
        biz, _ = self.get_business(request)
        serializer = MeetingConflictCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        conflicts = MeetingService.check_conflicts(
            business=biz,
            meeting_date=data['meeting_date'],
            start_time=data['start_time'],
            end_time=data['end_time'],
            organizer_user=request.user,
            employee_ids=data.get('participant_ids', []),
            exclude_meeting_id=data.get('exclude_meeting_id'),
            caller_user=request.user
        )

        return Response({
            'has_conflicts': len(conflicts) > 0,
            'conflicts_count': len(conflicts),
            'conflicts': conflicts
        })
