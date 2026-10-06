from zoneinfo import ZoneInfo
from datetime import date, timedelta
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

        day.centre = emp.branch
        day.save(update_fields=['centre', 'updated_at'])

        from apps.attendance.services.attendance_calculation_service import AttendanceCalculationService
        AttendanceCalculationService.calculate_daily_attendance(day, save=True)

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

        day.centre = emp.branch
        day.total_work_seconds += session_duration
        day.save(update_fields=['centre', 'total_work_seconds', 'updated_at'])

        from apps.attendance.services.attendance_calculation_service import AttendanceCalculationService
        AttendanceCalculationService.calculate_daily_attendance(day, save=True)

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
        import calendar as cal_mod
        from apps.organization.models import Holiday, Branch
        from apps.leaves.models import LeaveRequest

        ctx = get_user_context(request)
        emp = ctx.get('employee')

        emp_param = request.query_params.get('employee_id')
        if emp_param and (ctx['is_superadmin'] or ctx['role'] in [BusinessRole.BUSINESS_ADMIN, BusinessRole.MANAGER]):
            emp = Employee.objects.filter(id=emp_param).first()

        if not emp:
            raise ValidationError({'detail': 'Employee context required for attendance calendar.'})

        year = int(request.query_params.get('year', date.today().year))
        month = int(request.query_params.get('month', date.today().month))

        # Get all AttendanceDay records for the month
        qs = AttendanceDay.objects.filter(
            employee=emp,
            attendance_date__year=year,
            attendance_date__month=month
        ).prefetch_related('events').order_by('attendance_date')

        tz = get_employee_timezone(emp)

        # Build lookup from attendance records
        attendance_map = {}
        for day in qs:
            events = list(day.events.order_by('event_time'))
            first_in = next((e for e in events if e.event_type == AttendanceEventType.CHECK_IN), None)
            last_out = next((e for e in reversed(events) if e.event_type == AttendanceEventType.CHECK_OUT), None)
            verification = first_in.source if first_in else (last_out.source if last_out else 'WEB')
            loc = 'Centre'
            if first_in and first_in.latitude and first_in.longitude:
                loc = f"{first_in.latitude:.3f}, {first_in.longitude:.3f}"
            elif day.centre:
                loc = day.centre.name

            attendance_map[day.attendance_date] = {
                'status': day.status,
                'total_work_seconds': day.total_work_seconds,
                'work_hours': f"{day.total_work_seconds // 3600:02d}h {(day.total_work_seconds % 3600) // 60:02d}m",
                'overtime_seconds': day.overtime_seconds,
                'ot_hours': f"{day.overtime_seconds // 3600:02d}h {(day.overtime_seconds % 3600) // 60:02d}m",
                'check_in': first_in.event_time.astimezone(tz).strftime('%I:%M %p') if first_in else None,
                'check_out': last_out.event_time.astimezone(tz).strftime('%I:%M %p') if last_out else None,
                'verification_method': verification,
                'location': loc,
                'correction': day.notes or 'None',
            }

        # Build holiday lookup for this month
        biz = emp.business
        holiday_qs = Holiday.objects.filter(
            business=biz,
            holiday_date__year=year,
            holiday_date__month=month,
        )
        holiday_map = {}
        for h in holiday_qs:
            if h.applies_to_all_centres or (emp.branch and emp.branch in h.centres.all()):
                holiday_map[h.holiday_date] = h.name

        # Build approved leave lookup
        first_day = date(year, month, 1)
        last_day = date(year, month, cal_mod.monthrange(year, month)[1])
        leave_qs = LeaveRequest.objects.filter(
            employee=emp,
            status='APPROVED',
            start_date__lte=last_day,
            end_date__gte=first_day,
        ).select_related('leave_type')
        leave_map = {}
        for lr in leave_qs:
            d = max(lr.start_date, first_day)
            end = min(lr.end_date, last_day)
            while d <= end:
                leave_map[d] = lr.leave_type.name if lr.leave_type else 'Leave'
                d += timedelta(days=1)

        # Determine weekly off days from work schedule
        week_off_days = set()  # 0=Mon..6=Sun
        try:
            from django.db.models import Q
            from apps.attendance.models import EmployeeScheduleAssignment, WorkScheduleDay
            assignment = EmployeeScheduleAssignment.objects.filter(
                employee=emp,
                effective_from__lte=last_day,
            ).filter(
                Q(effective_to__isnull=True) | Q(effective_to__gte=first_day)
            ).select_related('schedule').order_by('-effective_from').first()
            if assignment:
                schedule_days = WorkScheduleDay.objects.filter(schedule=assignment.schedule)
                for sd in schedule_days:
                    if not sd.is_work_day:
                        week_off_days.add(sd.day_of_week)
        except Exception:
            pass

        today = date.today()
        num_days = cal_mod.monthrange(year, month)[1]
        calendar_data = []
        for day_num in range(1, num_days + 1):
            d = date(year, month, day_num)
            if d in attendance_map:
                entry = attendance_map[d]
                calendar_data.append({
                    'date': str(d),
                    'day': day_num,
                    'weekday': d.strftime('%a'),
                    'status': entry['status'],
                    'total_work_seconds': entry['total_work_seconds'],
                    'work_hours': entry['work_hours'],
                    'overtime_seconds': entry['overtime_seconds'],
                    'ot_hours': entry['ot_hours'],
                    'check_in': entry['check_in'],
                    'check_out': entry['check_out'],
                    'verification_method': entry.get('verification_method', 'WEB'),
                    'location': entry.get('location', 'Centre'),
                    'correction': entry.get('correction', 'None'),
                    'holiday_name': holiday_map.get(d),
                    'leave_type': leave_map.get(d),
                })
            else:
                # Determine status for days without attendance records
                if d in holiday_map:
                    fill_status = 'HOLIDAY'
                elif d.weekday() in week_off_days:
                    fill_status = 'WEEK_OFF'
                elif d in leave_map:
                    fill_status = 'LEAVE'
                elif d > today:
                    fill_status = 'FUTURE'
                elif d < today:
                    fill_status = 'ABSENT'
                else:
                    fill_status = 'NOT_MARKED'

                calendar_data.append({
                    'date': str(d),
                    'day': day_num,
                    'weekday': d.strftime('%a'),
                    'status': fill_status,
                    'total_work_seconds': 0,
                    'work_hours': '00h 00m',
                    'overtime_seconds': 0,
                    'ot_hours': '00h 00m',
                    'check_in': None,
                    'check_out': None,
                    'verification_method': '—',
                    'location': '—',
                    'correction': 'None',
                    'holiday_name': holiday_map.get(d),
                    'leave_type': leave_map.get(d),
                })

        # Summary counts
        total_ot_seconds = sum(day.overtime_seconds for day in qs)
        summary = {
            'present': sum(1 for d in calendar_data if d['status'] == 'PRESENT'),
            'absent': sum(1 for d in calendar_data if d['status'] == 'ABSENT'),
            'leave': sum(1 for d in calendar_data if d['status'] == 'LEAVE'),
            'holiday': sum(1 for d in calendar_data if d['status'] == 'HOLIDAY'),
            'week_off': sum(1 for d in calendar_data if d['status'] == 'WEEK_OFF'),
            'weekly_off': sum(1 for d in calendar_data if d['status'] == 'WEEK_OFF'),
            'half_day': sum(1 for d in calendar_data if d['status'] == 'HALF_DAY'),
            'late': sum(1 for d in calendar_data if d['status'] in ('LATE', 'LEAVE_EARLY')),
            'total_ot_seconds': total_ot_seconds,
            'ot_hours': round(total_ot_seconds / 3600, 1),
            'total_days': num_days,
        }

        return Response({
            'employee_id': str(emp.id),
            'employee_name': emp.full_name,
            'year': year,
            'month': month,
            'first_weekday': date(year, month, 1).weekday(),  # 0=Mon
            'days': calendar_data,
            'summary': summary,
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


class AttendanceDailyRegisterView(views.APIView):
    """
    Roster-style attendance register for a specific date across all or selected Centres.
    Lists ALL active employees. Employees with no punch records appear as NOT_MARKED,
    WEEKLY_OFF, HOLIDAY, or ON_LEAVE according to resolved Centre policies and calendars.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models import Q
        from datetime import datetime
        import zoneinfo

        ctx = get_user_context(request)
        biz = ctx['business']
        centre_id = request.query_params.get('centre_id') or request.query_params.get('branch_id')

        if ctx['is_superadmin']:
            biz_id = request.query_params.get('business_id')
            if biz_id:
                from apps.organization.models import Business
                biz = Business.objects.filter(id=biz_id).first() or biz
            elif not biz and centre_id and centre_id not in ['all', 'ALL', 'null', '']:
                from apps.organization.models import Branch
                b = Branch.resolve_branch(centre_id)
                if b:
                    biz = b.business
            elif not biz:
                from apps.organization.models import Business
                biz = Business.objects.filter(is_active=True).first()

        if not biz:
            raise PermissionDenied('No active business/enterprise context found.')

        # Resolve target date in enterprise/centre timezone
        tz_name = biz.timezone or 'Asia/Kolkata'
        try:
            biz_tz = zoneinfo.ZoneInfo(tz_name)
        except Exception:
            biz_tz = zoneinfo.ZoneInfo('Asia/Kolkata')

        date_param = request.query_params.get('date')
        if date_param:
            try:
                target_date = datetime.strptime(date_param, '%Y-%m-%d').date()
            except ValueError:
                target_date = timezone.now().astimezone(biz_tz).date()
        else:
            target_date = timezone.now().astimezone(biz_tz).date()

        # Query active employees
        emp_qs = Employee.objects.filter(business=biz, employment_status='ACTIVE').select_related(
            'branch', 'department', 'user', 'manager'
        )

        centre_id = request.query_params.get('centre_id') or request.query_params.get('branch_id')
        if centre_id and centre_id not in ['all', 'ALL', 'null', '']:
            from apps.organization.models import Branch
            branch_obj = Branch.resolve_branch(centre_id, business=biz)
            if branch_obj:
                emp_qs = emp_qs.filter(branch_id=branch_obj.id)
            else:
                emp_qs = emp_qs.none()

        # For Center Manager role, lock to their authorized center if not business admin
        if ctx['role'] == BusinessRole.MANAGER and ctx.get('employee') and ctx['employee'].branch_id:
            if not centre_id or centre_id in ['all', 'ALL']:
                emp_qs = emp_qs.filter(branch_id=ctx['employee'].branch_id)

        department_id = request.query_params.get('department_id')
        if department_id and department_id not in ['all', 'ALL', 'null', '']:
            emp_qs = emp_qs.filter(department_id=department_id)

        search_query = request.query_params.get('search') or request.query_params.get('employee')
        if search_query:
            emp_qs = emp_qs.filter(
                Q(first_name__icontains=search_query) |
                Q(last_name__icontains=search_query) |
                Q(employee_id__icontains=search_query) |
                Q(email__icontains=search_query)
            )

        employees = list(emp_qs.order_by('first_name', 'last_name'))
        if not employees:
            return Response({
                'date': str(target_date),
                'summary': {
                    'total_employees': 0, 'present': 0, 'late': 0, 'half_day': 0,
                    'leave_early': 0, 'absent': 0, 'on_leave': 0, 'holiday': 0,
                    'weekly_off': 0, 'not_marked': 0
                },
                'records': []
            })

        # Load holidays
        from apps.organization.models import Holiday
        from apps.leaves.models import LeaveRequest
        from apps.organization.services.policy_resolver import PolicyResolver
        from apps.attendance.services.attendance_calculation_service import AttendanceCalculationService

        holidays = list(Holiday.objects.filter(business=biz, holiday_date=target_date).prefetch_related('centres'))
        centre_holidays = [
            (h.applies_to_all_centres, {str(c.id) for c in h.centres.all()})
            for h in holidays
        ]

        # Load approved leaves
        leaves = LeaveRequest.objects.filter(
            business=biz,
            start_date__lte=target_date,
            end_date__gte=target_date,
            status='APPROVED',
            employee__in=employees
        ).select_related('leave_type')
        leaves_by_emp = {str(l.employee_id): l for l in leaves}

        # Load existing attendance days with events
        att_days = AttendanceDay.objects.filter(
            business=biz,
            attendance_date=target_date,
            employee__in=employees
        ).prefetch_related('events')
        days_by_emp = {str(d.employee_id): d for d in att_days}

        policies_cache = {}
        records = []
        summary = {
            'total_employees': len(employees),
            'present': 0,
            'late': 0,
            'half_day': 0,
            'leave_early': 0,
            'absent': 0,
            'on_leave': 0,
            'holiday': 0,
            'weekly_off': 0,
            'not_marked': 0
        }

        weekday_idx = target_date.weekday()

        for emp in employees:
            centre = emp.branch
            centre_key = str(centre.id) if centre else 'default'
            if centre_key not in policies_cache:
                policies_cache[centre_key] = PolicyResolver.get_attendance_policy(centre=centre, business=biz)['effective']
            policy = policies_cache[centre_key]

            # Employee timezone
            emp_tz_name = (centre.timezone if centre and getattr(centre, 'timezone', None) else tz_name) or 'Asia/Kolkata'
            try:
                emp_tz = zoneinfo.ZoneInfo(emp_tz_name)
            except Exception:
                emp_tz = biz_tz

            day = days_by_emp.get(str(emp.id))
            events = list(day.events.order_by('event_time')) if day else []

            if events:
                calc = AttendanceCalculationService.calculate_daily_attendance(day, effective_policy=policy, save=False)
                calc_status = calc['status']

                first_in = next((e for e in events if e.event_type == AttendanceEventType.CHECK_IN), None)
                last_out = next((e for e in reversed(events) if e.event_type == AttendanceEventType.CHECK_OUT), None)
                check_in_str = first_in.event_time.astimezone(emp_tz).strftime('%I:%M %p') if first_in else '—'
                check_out_str = last_out.event_time.astimezone(emp_tz).strftime('%I:%M %p') if last_out else '—'
                work_sec = calc['total_work_seconds']
                work_hrs_str = f"{work_sec // 3600:02d}h {(work_sec % 3600) // 60:02d}m"
                late_m = calc['late_minutes']
                early_m = calc['early_leave_minutes']
                ot_s = calc['overtime_seconds']
                final_status = calc_status
            else:
                check_in_str = '—'
                check_out_str = '—'
                work_hrs_str = '—'
                late_m = 0
                early_m = 0
                ot_s = 0

                # Check approved leave
                if str(emp.id) in leaves_by_emp:
                    final_status = 'ON_LEAVE'
                else:
                    # Check holiday
                    emp_centre_id = str(centre.id) if centre else None
                    is_holiday = any(all_centres or (emp_centre_id in c_ids) for all_centres, c_ids in centre_holidays)
                    if is_holiday:
                        final_status = 'HOLIDAY'
                    else:
                        # Check weekly off days
                        w_days = policy.get('weekly_off_days')
                        if w_days is not None and isinstance(w_days, list):
                            is_off = weekday_idx in [int(x) for x in w_days]
                        else:
                            is_off = (weekday_idx == int(policy.get('weekly_off', 6)))

                        if is_off:
                            final_status = 'WEEKLY_OFF'
                        else:
                            final_status = 'NOT_MARKED'

            # Update summary counter
            key_map = {
                'PRESENT': 'present',
                'LATE': 'late',
                'HALF_DAY': 'half_day',
                'LEAVE_EARLY': 'leave_early',
                'OVERTIME': 'present',
                'ABSENT': 'absent',
                'ON_LEAVE': 'on_leave',
                'HOLIDAY': 'holiday',
                'WEEK_OFF': 'weekly_off',
                'WEEKLY_OFF': 'weekly_off',
                'NOT_MARKED': 'not_marked',
            }
            mapped_key = key_map.get(final_status, 'not_marked')
            summary[mapped_key] = summary.get(mapped_key, 0) + 1

            record = {
                'id': str(day.id) if day else None,
                'employee_id': str(emp.id),
                'employee_code': emp.employee_id,
                'employee_name': emp.full_name,
                'department_name': emp.department.name if emp.department else '—',
                'designation_name': emp.designation if emp.designation else '—',
                'centre_id': str(centre.id) if centre else None,
                'centre_name': centre.name if centre else '—',
                'attendance_date': str(target_date),
                'check_in': check_in_str,
                'check_out': check_out_str,
                'work_hours': work_hrs_str,
                'status': 'WEEKLY_OFF' if final_status == 'WEEK_OFF' else final_status,
                'late_minutes': late_m,
                'early_leave_minutes': early_m,
                'overtime_seconds': ot_s,
                'events_count': len(events),
            }
            records.append(record)

        # Optional status filtering
        status_filter = request.query_params.get('status')
        if status_filter and status_filter not in ['all', 'ALL', 'null', '']:
            status_filter_upper = status_filter.upper()
            if status_filter_upper == 'WEEK_OFF':
                status_filter_upper = 'WEEKLY_OFF'
            records = [r for r in records if r['status'] == status_filter_upper]

        return Response({
            'date': str(target_date),
            'summary': summary,
            'records': records
        })

