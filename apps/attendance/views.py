from zoneinfo import ZoneInfo
from datetime import date
from django.utils import timezone
from rest_framework import status, views
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.permissions import get_user_context
from apps.organization.models import BusinessRole, Employee
from apps.attendance.models import (
    AttendanceDay, AttendanceEvent, AttendanceStatus,
    AttendanceEventType, AttendanceEventSource
)
from apps.attendance.serializers import AttendanceDaySerializer, AttendanceEventSerializer


def get_employee_timezone(employee):
    biz = employee.business
    tz_str = (employee.branch.timezone if employee.branch and employee.branch.timezone else biz.timezone) or 'UTC'
    try:
        return ZoneInfo(tz_str)
    except Exception:
        return ZoneInfo('UTC')


def get_today_state(employee):
    tz = get_employee_timezone(employee)
    server_now = timezone.now()
    local_now = server_now.astimezone(tz)
    local_date = local_now.date()

    day, _ = AttendanceDay.objects.get_or_create(
        business=employee.business,
        employee=employee,
        attendance_date=local_date,
        defaults={'status': AttendanceStatus.PRESENT}
    )

    events = list(day.events.order_by('event_time'))
    last_event = events[-1] if events else None

    # Determine current status
    is_checked_in = (last_event.event_type == AttendanceEventType.CHECK_IN) if last_event else False

    # Calculate active working seconds so far
    accumulated_seconds = day.total_work_seconds
    current_session_seconds = 0
    if is_checked_in and last_event:
        current_session_seconds = int((server_now - last_event.event_time).total_seconds())

    first_check_in = next((e for e in events if e.event_type == AttendanceEventType.CHECK_IN), None)
    last_check_out = next((e for e in reversed(events) if e.event_type == AttendanceEventType.CHECK_OUT), None)

    return {
        'attendance_day_id': str(day.id),
        'attendance_date': str(local_date),
        'day_status': day.status,
        'is_checked_in': is_checked_in,
        'total_work_seconds': accumulated_seconds + current_session_seconds,
        'accumulated_seconds': accumulated_seconds,
        'first_check_in_time': first_check_in.event_time.astimezone(tz).strftime('%I:%M %p') if first_check_in else None,
        'last_check_out_time': last_check_out.event_time.astimezone(tz).strftime('%I:%M %p') if last_check_out else None,
        'last_event_type': last_event.event_type if last_event else None,
        'events': AttendanceEventSerializer(events, many=True).data,
    }


class AttendanceTodayView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        if ctx.get('role') == BusinessRole.BROKER:
            raise PermissionDenied('Brokers do not have access to attendance operations.')
        emp = ctx.get('employee')
        if not emp:
            # Allow manager or business admin to inspect a specific employee via query param
            emp_id = request.query_params.get('employee_id')
            if emp_id and (ctx['is_superadmin'] or ctx['role'] in [BusinessRole.BUSINESS_ADMIN, BusinessRole.MANAGER]):
                emp = Employee.objects.filter(id=emp_id).first()

        if not emp:
            raise ValidationError({'detail': 'No associated employee profile found for user.'})

        state = get_today_state(emp)
        return Response(state)


class AttendanceCheckInView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ctx = get_user_context(request)
        emp = ctx.get('employee')
        if not emp:
            raise PermissionDenied('Only staff/employees with a linked profile can record attendance.')

        tz = get_employee_timezone(emp)
        server_now = timezone.now()
        local_date = server_now.astimezone(tz).date()

        day, _ = AttendanceDay.objects.get_or_create(
            business=emp.business,
            employee=emp,
            attendance_date=local_date,
            defaults={'status': AttendanceStatus.PRESENT}
        )

        last_event = day.events.order_by('event_time').last()
        if last_event and last_event.event_type == AttendanceEventType.CHECK_IN:
            raise ValidationError({'detail': 'Already checked in. Please check out first before checking in again.'})

        lat = request.data.get('latitude')
        lng = request.data.get('longitude')
        accuracy = request.data.get('location_accuracy')
        device_id = request.data.get('device_id', '')
        source = request.data.get('source', AttendanceEventSource.WEB)

        AttendanceEvent.objects.create(
            business=emp.business,
            attendance_day=day,
            employee=emp,
            event_type=AttendanceEventType.CHECK_IN,
            event_time=server_now,
            latitude=lat if lat is not None else None,
            longitude=lng if lng is not None else None,
            location_accuracy=accuracy if accuracy is not None else None,
            device_id=device_id,
            source=source,
            notes=request.data.get('notes', '')
        )

        day.status = AttendanceStatus.PRESENT
        day.save(update_fields=['status', 'updated_at'])

        state = get_today_state(emp)
        return Response({
            'detail': 'Check-in recorded successfully.',
            **state
        }, status=status.HTTP_201_CREATED)


class AttendanceCheckOutView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ctx = get_user_context(request)
        emp = ctx.get('employee')
        if not emp:
            raise PermissionDenied('Only staff/employees with a linked profile can record attendance.')

        tz = get_employee_timezone(emp)
        server_now = timezone.now()
        local_date = server_now.astimezone(tz).date()

        day = AttendanceDay.objects.filter(
            business=emp.business,
            employee=emp,
            attendance_date=local_date
        ).first()

        if not day:
            raise ValidationError({'detail': 'No check-in record found for today. Cannot check out.'})

        last_event = day.events.order_by('event_time').last()
        if not last_event or last_event.event_type != AttendanceEventType.CHECK_IN:
            raise ValidationError({'detail': 'Cannot check out without an active check-in session.'})

        # Calculate session elapsed time
        session_duration = max(0, int((server_now - last_event.event_time).total_seconds()))

        lat = request.data.get('latitude')
        lng = request.data.get('longitude')
        accuracy = request.data.get('location_accuracy')
        device_id = request.data.get('device_id', '')
        source = request.data.get('source', AttendanceEventSource.WEB)

        AttendanceEvent.objects.create(
            business=emp.business,
            attendance_day=day,
            employee=emp,
            event_type=AttendanceEventType.CHECK_OUT,
            event_time=server_now,
            latitude=lat if lat is not None else None,
            longitude=lng if lng is not None else None,
            location_accuracy=accuracy if accuracy is not None else None,
            device_id=device_id,
            source=source,
            notes=request.data.get('notes', '')
        )

        day.total_work_seconds += session_duration
        day.save(update_fields=['total_work_seconds', 'updated_at'])

        state = get_today_state(emp)
        return Response({
            'detail': 'Check-out recorded successfully.',
            **state
        }, status=status.HTTP_200_OK)


class AttendanceHistoryView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']

        if ctx['is_superadmin']:
            qs = AttendanceDay.objects.all()
            biz_id = request.query_params.get('business_id')
            if biz_id:
                qs = qs.filter(business_id=biz_id)
        elif ctx['role'] == BusinessRole.BUSINESS_ADMIN:
            qs = AttendanceDay.objects.filter(business=biz)
        elif ctx['role'] == BusinessRole.MANAGER:
            if ctx.get('employee'):
                qs = AttendanceDay.objects.filter(
                    business=biz,
                    employee__manager=ctx['employee']
                )
            else:
                qs = AttendanceDay.objects.none()
        else:
            # Staff
            if ctx.get('employee'):
                qs = AttendanceDay.objects.filter(employee=ctx['employee'])
            else:
                qs = AttendanceDay.objects.none()

        emp_filter = request.query_params.get('employee_id')
        if emp_filter:
            qs = qs.filter(employee_id=emp_filter)

        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
        if date_from:
            qs = qs.filter(attendance_date__gte=date_from)
        if date_to:
            qs = qs.filter(attendance_date__lte=date_to)

        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter.upper())

        qs = qs.select_related('employee').prefetch_related('events').order_by('-attendance_date')
        return Response(AttendanceDaySerializer(qs[:100], many=True).data)


class AttendanceCalendarView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        emp = ctx.get('employee')

        emp_param = request.query_params.get('employee_id')
        if emp_param and (ctx['is_superadmin'] or ctx['role'] in [BusinessRole.BUSINESS_ADMIN, BusinessRole.MANAGER]):
            emp = Employee.objects.filter(id=emp_param).first()

        if not emp:
            raise ValidationError({'detail': 'Employee context required for attendance calendar.'})

        year = int(request.query_params.get('year', date.today().year))
        month = int(request.query_params.get('month', date.today().month))

        qs = AttendanceDay.objects.filter(
            employee=emp,
            attendance_date__year=year,
            attendance_date__month=month
        ).prefetch_related('events').order_by('attendance_date')

        tz = get_employee_timezone(emp)
        calendar_data = []
        for day in qs:
            events = list(day.events.order_by('event_time'))
            first_in = next((e for e in events if e.event_type == AttendanceEventType.CHECK_IN), None)
            last_out = next((e for e in reversed(events) if e.event_type == AttendanceEventType.CHECK_OUT), None)

            calendar_data.append({
                'date': str(day.attendance_date),
                'status': day.status,
                'total_work_seconds': day.total_work_seconds,
                'work_hours': f"{day.total_work_seconds // 3600:02d}h {(day.total_work_seconds % 3600) // 60:02d}m",
                'check_in': first_in.event_time.astimezone(tz).strftime('%I:%M %p') if first_in else None,
                'check_out': last_out.event_time.astimezone(tz).strftime('%I:%M %p') if last_out else None,
            })

        return Response({
            'employee_id': str(emp.id),
            'employee_name': emp.full_name,
            'year': year,
            'month': month,
            'days': calendar_data,
        })


class WorkScheduleListView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if ctx['is_superadmin']:
            biz_id = request.query_params.get('business_id')
            if biz_id:
                from apps.organization.models import Business
                biz = Business.objects.filter(id=biz_id).first()

        from apps.attendance.models import WorkSchedule
        from apps.attendance.serializers import WorkScheduleSerializer
        qs = WorkSchedule.objects.filter(business=biz, is_active=True) if biz else WorkSchedule.objects.none()
        return Response(WorkScheduleSerializer(qs, many=True).data)

    def post(self, request):
        ctx = get_user_context(request)
        if not (ctx['is_superadmin'] or ctx['role'] == BusinessRole.BUSINESS_ADMIN):
            raise PermissionDenied('Only Business Admin can configure work schedules.')

        biz = ctx['business']
        if ctx['is_superadmin']:
            biz_id = request.data.get('business_id')
            if biz_id:
                from apps.organization.models import Business
                biz = Business.objects.filter(id=biz_id).first()

        from apps.attendance.serializers import WorkScheduleSerializer
        serializer = WorkScheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        schedule = serializer.save(business=biz)
        return Response(WorkScheduleSerializer(schedule).data, status=status.HTTP_201_CREATED)


class AttendanceCorrectionListCreateView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        from apps.attendance.models import AttendanceCorrection
        from apps.attendance.serializers import AttendanceCorrectionSerializer

        if ctx['is_superadmin']:
            qs = AttendanceCorrection.objects.all()
        elif ctx['role'] == BusinessRole.BUSINESS_ADMIN:
            qs = AttendanceCorrection.objects.filter(business=biz)
        elif ctx['role'] == BusinessRole.MANAGER and ctx.get('employee'):
            qs = AttendanceCorrection.objects.filter(business=biz, attendance_day__employee__manager=ctx['employee'])
        else:
            qs = AttendanceCorrection.objects.filter(requested_by=request.user)

        status_param = request.query_params.get('status')
        if status_param:
            qs = qs.filter(status=status_param.upper())

        return Response(AttendanceCorrectionSerializer(qs[:100], many=True).data)

    def post(self, request):
        ctx = get_user_context(request)
        attendance_day_id = request.data.get('attendance_day_id')
        correction_type = request.data.get('correction_type', 'STATUS')
        reason = request.data.get('reason', '')
        corrected_status = request.data.get('corrected_status', '')
        corrected_time = request.data.get('corrected_time', None)

        from apps.attendance.models import AttendanceDay, AttendanceCorrection
        from apps.attendance.serializers import AttendanceCorrectionSerializer

        day = AttendanceDay.objects.filter(id=attendance_day_id).select_related('business', 'employee').first()
        if not day:
            raise NotFound('Attendance day record not found.')

        correction = AttendanceCorrection.objects.create(
            business=day.business,
            attendance_day=day,
            requested_by=request.user,
            correction_type=correction_type,
            reason=reason,
            corrected_status=corrected_status,
            corrected_time=corrected_time,
            status='PENDING'
        )
        return Response(AttendanceCorrectionSerializer(correction).data, status=status.HTTP_201_CREATED)


class AttendanceCorrectionApproveView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        ctx = get_user_context(request)
        from apps.attendance.models import AttendanceCorrection
        from apps.attendance.serializers import AttendanceCorrectionSerializer

        correction = AttendanceCorrection.objects.filter(id=pk).select_related('attendance_day', 'business').first()
        if not correction:
            raise NotFound('Correction request not found.')

        # Permission verification
        can_approve = False
        if ctx['is_superadmin']:
            can_approve = True
        elif ctx['role'] == BusinessRole.BUSINESS_ADMIN and correction.business_id == ctx['business'].id:
            can_approve = True
        elif ctx['role'] == BusinessRole.MANAGER and ctx.get('employee') and correction.attendance_day.employee.manager_id == ctx['employee'].id:
            can_approve = True

        if not can_approve:
            raise PermissionDenied('Permission denied to approve attendance correction.')

        # Apply correction to AttendanceDay
        if correction.corrected_status:
            correction.attendance_day.status = correction.corrected_status
            correction.attendance_day.save(update_fields=['status'])

        correction.status = 'APPROVED'
        correction.reviewed_by = request.user
        correction.reviewed_at = timezone.now()
        correction.save(update_fields=['status', 'reviewed_by', 'reviewed_at'])

        return Response({
            'detail': 'Attendance correction approved.',
            'correction': AttendanceCorrectionSerializer(correction).data
        })


class AttendanceCorrectionRejectView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        ctx = get_user_context(request)
        from apps.attendance.models import AttendanceCorrection
        from apps.attendance.serializers import AttendanceCorrectionSerializer

        correction = AttendanceCorrection.objects.filter(id=pk).first()
        if not correction:
            raise NotFound('Correction request not found.')

        correction.status = 'REJECTED'
        correction.reviewed_by = request.user
        correction.reviewed_at = timezone.now()
        correction.review_notes = request.data.get('reason', '')
        correction.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_notes'])

        return Response({
            'detail': 'Attendance correction rejected.',
            'correction': AttendanceCorrectionSerializer(correction).data
        })

