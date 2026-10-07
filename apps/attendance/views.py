from zoneinfo import ZoneInfo
from datetime import date, timedelta
from django.utils import timezone
from rest_framework import status, views
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.permissions import get_user_context
from apps.organization.models import BusinessRole, Employee, Branch
from apps.organization.services.policy_resolver import PolicyResolver
from apps.organization.services.permission_service import PermissionService
from apps.attendance.models import (
    AttendanceDay, AttendanceEvent, AttendanceStatus,
    AttendanceEventType, AttendanceEventSource, AttendanceMethod
)
from apps.attendance.services.attendance_service import AttendanceService
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

        state = AttendanceService.get_today_state(emp)

        # Include permitted attendance methods & verification requirements for employee's centre
        centre = emp.branch
        biz = emp.business
        policy_data = PolicyResolver.get_attendance_policy(centre=centre, business=biz, user=request.user)
        eff = policy_data.get('effective', {})
        allowed_methods = {
            'normal_punch': eff.get('allow_normal_punch', True),
            'qr': eff.get('allow_qr', False),
            'face_recognition': eff.get('allow_face_recognition', False),
            'location_required': bool(eff.get('location_required_checkin') or eff.get('allow_geofencing')),
            'geofence_radius': eff.get('gps_radius_meters', 100),
            'centre_latitude': eff.get('gps_latitude') or (float(centre.latitude) if centre and centre.latitude is not None else None),
            'centre_longitude': eff.get('gps_longitude') or (float(centre.longitude) if centre and centre.longitude is not None else None),
        }
        return Response({
            **state,
            'allowed_methods': allowed_methods,
            'centre_id': str(centre.id) if centre else None,
            'centre_name': centre.name if centre else '—',
        })


class AttendanceCheckInView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.attendance.services.attendance_service import AttendanceService
        method = request.data.get('attendance_method') or AttendanceMethod.NORMAL
        lat = request.data.get('latitude')
        lng = request.data.get('longitude')
        accuracy = request.data.get('location_accuracy')
        device_id = request.data.get('device_id', '')
        source = request.data.get('source', AttendanceEventSource.WEB)
        qr_code = request.data.get('qr_code')
        face_data = request.data.get('face_data')
        notes = request.data.get('notes', '')
        target_emp_id = request.data.get('employee_id')

        state = AttendanceService.record_punch(
            user=request.user,
            punch_type='CHECK_IN',
            attendance_method=method,
            latitude=lat,
            longitude=lng,
            location_accuracy=accuracy,
            device_id=device_id,
            source=source,
            qr_code=qr_code,
            face_data=face_data,
            notes=notes,
            target_employee_id=target_emp_id
        )

        return Response({
            'detail': 'Check-in recorded successfully.',
            **state
        }, status=status.HTTP_201_CREATED)


class AttendanceCheckOutView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.attendance.services.attendance_service import AttendanceService
        method = request.data.get('attendance_method') or AttendanceMethod.NORMAL
        lat = request.data.get('latitude')
        lng = request.data.get('longitude')
        accuracy = request.data.get('location_accuracy')
        device_id = request.data.get('device_id', '')
        source = request.data.get('source', AttendanceEventSource.WEB)
        qr_code = request.data.get('qr_code')
        face_data = request.data.get('face_data')
        notes = request.data.get('notes', '')
        target_emp_id = request.data.get('employee_id')

        state = AttendanceService.record_punch(
            user=request.user,
            punch_type='CHECK_OUT',
            attendance_method=method,
            latitude=lat,
            longitude=lng,
            location_accuracy=accuracy,
            device_id=device_id,
            source=source,
            qr_code=qr_code,
            face_data=face_data,
            notes=notes,
            target_employee_id=target_emp_id
        )

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

            # If record was manually overridden by Manager/Admin, respect manual values
            if day and day.is_overridden:
                final_status = day.status
                if day.check_in:
                    check_in_str = day.check_in.astimezone(emp_tz).strftime('%I:%M %p')
                if day.check_out:
                    check_out_str = day.check_out.astimezone(emp_tz).strftime('%I:%M %p')
                work_sec = day.total_work_seconds
                work_hrs_str = f"{work_sec // 3600:02d}h {(work_sec % 3600) // 60:02d}m"
                ot_s = day.overtime_seconds

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
                'total_work_seconds': work_sec if 'work_sec' in locals() else 0,
                'status': 'WEEKLY_OFF' if final_status == 'WEEK_OFF' else final_status,
                'late_minutes': late_m,
                'early_leave_minutes': early_m,
                'overtime_seconds': ot_s,
                'overtime_hours': f"{ot_s // 3600:02d}h {(ot_s % 3600) // 60:02d}m",
                'attendance_method': day.attendance_method if day else '—',
                'location_verified': day.location_verified if day else False,
                'is_overridden': day.is_overridden if day else False,
                'overridden_by_name': day.overridden_by.full_name if day and day.overridden_by else None,
                'overridden_at': day.overridden_at.isoformat() if day and day.overridden_at else None,
                'override_reason': day.override_reason if day else '',
                'original_check_in': day.original_check_in.astimezone(emp_tz).strftime('%I:%M %p') if day and day.original_check_in else None,
                'original_check_out': day.original_check_out.astimezone(emp_tz).strftime('%I:%M %p') if day and day.original_check_out else None,
                'original_status': day.original_status if day else None,
                'events_count': len(events),
            }
            records.append(record)

        # Calculate total hours and overtime across records
        total_work_sec = sum(r['total_work_seconds'] for r in records)
        total_ot_sec = sum(r['overtime_seconds'] for r in records)
        summary['total_working_hours'] = round(total_work_sec / 3600, 1)
        summary['total_overtime_hours'] = round(total_ot_sec / 3600, 1)

        # Optional method filtering
        method_filter = request.query_params.get('method') or request.query_params.get('attendance_method')
        if method_filter and method_filter not in ['all', 'ALL', 'null', '']:
            records = [r for r in records if r['attendance_method'] == method_filter.upper()]

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


class AttendanceRecordDetailView(views.APIView):
    """
    Returns complete details of a specific AttendanceDay record including:
    - Employee profile
    - Timestamps, working hours, and overtime
    - Punch method & location verification metadata
    - Manager override state (original vs override, reason, overridden by)
    - Full raw events audit trail
    - AuditLog history
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from apps.attendance.models import AttendanceDay
        from apps.attendance.serializers import AttendanceDaySerializer, AttendanceEventSerializer
        from apps.core.models import AuditLog
        from apps.core.serializers import AuditLogSerializer

        day = AttendanceDay.objects.filter(id=pk).select_related(
            'employee', 'employee__branch', 'employee__department',
            'centre', 'business', 'overridden_by'
        ).prefetch_related('events').first()

        if not day:
            raise NotFound('Attendance record not found.')

        ctx = get_user_context(request)
        if not ctx['is_superadmin'] and day.business_id != ctx['business'].id:
            raise PermissionDenied('Cross-tenant access forbidden.')

        emp = day.employee
        centre = day.centre or emp.branch
        tz = AttendanceService.get_employee_timezone(emp)

        events_data = AttendanceEventSerializer(day.events.order_by('event_time'), many=True).data

        audit_logs = AuditLog.objects.filter(
            entity_type='AttendanceDay',
            entity_id=str(day.id)
        ).select_related('actor').order_by('-created_at')[:20]

        return Response({
            'id': str(day.id),
            'attendance_date': str(day.attendance_date),
            'status': day.status,
            'attendance_method': day.attendance_method,
            'location_verified': day.location_verified,
            'verification_metadata': day.verification_metadata,
            'check_in': day.check_in.astimezone(tz).strftime('%I:%M %p') if day.check_in else None,
            'check_out': day.check_out.astimezone(tz).strftime('%I:%M %p') if day.check_out else None,
            'check_in_raw': day.check_in.isoformat() if day.check_in else None,
            'check_out_raw': day.check_out.isoformat() if day.check_out else None,
            'total_work_seconds': day.total_work_seconds,
            'work_hours_display': f"{day.total_work_seconds // 3600:02d}h {(day.total_work_seconds % 3600) // 60:02d}m",
            'overtime_seconds': day.overtime_seconds,
            'ot_hours_display': f"{day.overtime_seconds // 3600:02d}h {(day.overtime_seconds % 3600) // 60:02d}m",
            'late_minutes': day.late_minutes,
            'early_leave_minutes': day.early_leave_minutes,
            'is_locked': day.is_locked,
            'notes': day.notes,
            # Override Info
            'is_overridden': day.is_overridden,
            'overridden_by_id': str(day.overridden_by.id) if day.overridden_by else None,
            'overridden_by_name': day.overridden_by.full_name if day.overridden_by else None,
            'overridden_at': day.overridden_at.isoformat() if day.overridden_at else None,
            'override_reason': day.override_reason,
            'original_check_in': day.original_check_in.astimezone(tz).strftime('%I:%M %p') if day.original_check_in else None,
            'original_check_out': day.original_check_out.astimezone(tz).strftime('%I:%M %p') if day.original_check_out else None,
            'original_status': day.original_status,
            # Employee Info
            'employee': {
                'id': str(emp.id),
                'code': emp.employee_id,
                'name': emp.full_name,
                'email': emp.email,
                'phone': emp.phone,
                'designation': emp.designation,
                'department': emp.department.name if emp.department else '—',
                'centre_id': str(centre.id) if centre else None,
                'centre_name': centre.name if centre else '—',
            },
            'events': events_data,
            'audit_history': AuditLogSerializer(audit_logs, many=True).data,
            'created_at': day.created_at.isoformat(),
            'updated_at': day.updated_at.isoformat(),
        })


class AttendanceRecordOverrideView(views.APIView):
    """
    Manager / Admin Manual Edit and Override:
    Allows authorized roles to edit check_in, check_out, status, overtime, and notes.
    Strictly requires a reason and writes an immutable audit record.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        from apps.attendance.models import AttendanceDay
        from apps.attendance.services.attendance_service import AttendanceService

        day = AttendanceDay.objects.filter(id=pk).select_related('employee', 'centre', 'business').first()
        if not day:
            raise NotFound('Attendance record not found.')

        ctx = get_user_context(request)
        biz = day.business
        centre = day.centre or day.employee.branch

        # Authorization check via RBAC
        can_override = False
        if request.user.is_superuser:
            can_override = True
        elif ctx['role'] == BusinessRole.BUSINESS_ADMIN and day.business_id == ctx['business'].id:
            can_override = True
        elif ctx['role'] == BusinessRole.MANAGER:
            can_override = PermissionService.has_permission(
                user=request.user,
                permission_key='attendance.edit',
                business=biz,
                centre=centre,
                target_employee=day.employee
            )

        if not can_override:
            raise PermissionDenied('You do not have permission to manually edit or override attendance records.')

        check_in_time = request.data.get('check_in')
        check_out_time = request.data.get('check_out')
        status_val = request.data.get('status')
        overtime_sec = request.data.get('overtime_seconds')
        reason = request.data.get('reason')
        notes = request.data.get('notes', '')

        if not reason or not str(reason).strip():
            raise ValidationError({'detail': 'A mandatory reason is required to edit or override attendance.'})

        updated_day = AttendanceService.override_attendance(
            user=request.user,
            attendance_day=day,
            check_in_time=check_in_time,
            check_out_time=check_out_time,
            status=status_val,
            overtime_seconds=overtime_sec,
            reason=str(reason).strip(),
            notes=notes
        )

        return Response({
            'detail': 'Attendance record overridden successfully.',
            'id': str(updated_day.id),
            'status': updated_day.status,
            'is_overridden': updated_day.is_overridden,
            'override_reason': updated_day.override_reason,
            'total_work_seconds': updated_day.total_work_seconds,
            'overtime_seconds': updated_day.overtime_seconds,
        })


class AttendanceQRTokenView(views.APIView):
    """
    Generates a secure QR payload for a centre.
    Used for QR Attendance kiosks or centre check-in posters.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        import time as time_lib
        ctx = get_user_context(request)
        biz = ctx['business']
        centre_id = request.query_params.get('centre_id')

        centre = None
        if centre_id and centre_id not in ['all', 'ALL', 'null', '']:
            centre = Branch.objects.filter(id=centre_id, business=biz).first()
        elif ctx.get('employee') and ctx['employee'].branch:
            centre = ctx['employee'].branch
        else:
            centre = Branch.objects.filter(business=biz, is_active=True).first()

        if not centre:
            raise NotFound('No active centre available for QR generation.')

        ts = int(time_lib.time())
        token = f"OWNMANAGE:CENTRE:{centre.id}:{ts}"

        return Response({
            'centre_id': str(centre.id),
            'centre_name': centre.name,
            'qr_code': token,
            'timestamp': ts,
            'expires_in_seconds': 300,
        })


class AttendanceMonthlyRegisterView(views.APIView):
    """
    Matrix-style monthly attendance view across all employees.
    Provides daily statuses for each date (1..N).
    Employees without punch records appear as NOT_MARKED (or HOLIDAY, WEEKLY_OFF, ON_LEAVE).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        import calendar as cal_mod
        from apps.organization.models import Holiday
        from apps.leaves.models import LeaveRequest
        from apps.attendance.models import AttendanceDay

        ctx = get_user_context(request)
        biz = ctx['business']
        centre_id = request.query_params.get('centre_id') or request.query_params.get('branch_id')

        if ctx['is_superadmin'] and not biz:
            biz_id = request.query_params.get('business_id')
            if biz_id:
                biz = Business.objects.filter(id=biz_id).first()
            elif centre_id and centre_id not in ['all', 'ALL', 'null', '']:
                b = Branch.resolve_branch(centre_id)
                if b:
                    biz = b.business
            if not biz:
                biz = Business.objects.filter(is_active=True).first()

        if not biz:
            raise PermissionDenied('No active enterprise context found.')

        today = date.today()
        year = int(request.query_params.get('year', today.year))
        month = int(request.query_params.get('month', today.month))
        num_days = cal_mod.monthrange(year, month)[1]

        # Employees query
        emp_qs = Employee.objects.filter(business=biz, employment_status='ACTIVE').select_related(
            'branch', 'department'
        )

        if centre_id and centre_id not in ['all', 'ALL', 'null', '']:
            branch_obj = Branch.resolve_branch(centre_id, business=biz)
            if branch_obj:
                emp_qs = emp_qs.filter(branch_id=branch_obj.id)
            else:
                emp_qs = emp_qs.none()
        elif ctx['role'] == BusinessRole.MANAGER and ctx.get('employee') and ctx['employee'].branch_id:
            emp_qs = emp_qs.filter(branch_id=ctx['employee'].branch_id)

        search_query = request.query_params.get('search')
        if search_query:
            from django.db.models import Q
            emp_qs = emp_qs.filter(
                Q(first_name__icontains=search_query) |
                Q(last_name__icontains=search_query) |
                Q(employee_id__icontains=search_query)
            )

        employees = list(emp_qs.order_by('first_name', 'last_name'))

        first_date = date(year, month, 1)
        last_date = date(year, month, num_days)

        # Bulk load holidays
        holidays = list(Holiday.objects.filter(business=biz, holiday_date__gte=first_date, holiday_date__lte=last_date).prefetch_related('centres'))

        # Bulk load leaves
        leaves = list(LeaveRequest.objects.filter(
            business=biz,
            status='APPROVED',
            start_date__lte=last_date,
            end_date__gte=first_date,
            employee__in=employees
        ).select_related('leave_type'))

        # Bulk load attendance days with events
        att_days = list(AttendanceDay.objects.filter(
            business=biz,
            attendance_date__gte=first_date,
            attendance_date__lte=last_date,
            employee__in=employees
        ).prefetch_related('events'))

        # Build quick lookup tables
        day_records_map = {}
        for d in att_days:
            day_records_map[(str(d.employee_id), d.attendance_date)] = d

        emp_leaves_map = {}
        for l in leaves:
            e_id = str(l.employee_id)
            if e_id not in emp_leaves_map:
                emp_leaves_map[e_id] = []
            emp_leaves_map[e_id].append(l)

        policies_cache = {}
        emp_rows = []
        overall_summary = {
            'total_employees': len(employees),
            'present': 0, 'late': 0, 'half_day': 0, 'leave_early': 0,
            'absent': 0, 'on_leave': 0, 'holiday': 0, 'weekly_off': 0, 'not_marked': 0,
            'total_hours': 0, 'total_ot_hours': 0
        }

        # Build days header
        days_header = []
        for d_num in range(1, num_days + 1):
            curr_d = date(year, month, d_num)
            days_header.append({
                'day': d_num,
                'date': str(curr_d),
                'weekday': curr_d.strftime('%a'),
            })

        for emp in employees:
            centre = emp.branch
            centre_key = str(centre.id) if centre else 'default'
            if centre_key not in policies_cache:
                policies_cache[centre_key] = PolicyResolver.get_attendance_policy(centre=centre, business=biz)['effective']
            policy = policies_cache[centre_key]

            emp_days_data = {}
            emp_summary = {
                'present': 0, 'late': 0, 'half_day': 0, 'leave_early': 0,
                'absent': 0, 'on_leave': 0, 'holiday': 0, 'weekly_off': 0, 'not_marked': 0,
                'total_seconds': 0, 'ot_seconds': 0
            }

            for d_num in range(1, num_days + 1):
                cal_date = date(year, month, d_num)
                w_idx = cal_date.weekday()
                day_record = day_records_map.get((str(emp.id), cal_date))

                if day_record:
                    status_val = day_record.status
                    work_sec = day_record.total_work_seconds
                    ot_sec = day_record.overtime_seconds
                    emp_days_data[d_num] = {
                        'status': status_val,
                        'record_id': str(day_record.id),
                        'work_hours': f"{work_sec // 3600:02d}h {(work_sec % 3600) // 60:02d}m",
                        'ot_hours': f"{ot_sec // 3600:02d}h {(ot_sec % 3600) // 60:02d}m",
                        'method': day_record.attendance_method,
                        'location_verified': day_record.location_verified,
                        'is_overridden': day_record.is_overridden,
                    }
                else:
                    # Determine absence / holiday / leave / weekly off / NOT_MARKED
                    # Check approved leave
                    user_leaves = emp_leaves_map.get(str(emp.id), [])
                    is_on_leave = any(l.start_date <= cal_date <= l.end_date for l in user_leaves)

                    # Check holiday
                    emp_centre_id = str(centre.id) if centre else None
                    is_holiday = any(
                        h.holiday_date == cal_date and (h.applies_to_all_centres or (emp_centre_id and any(str(c.id) == emp_centre_id for c in h.centres.all())))
                        for h in holidays
                    )

                    # Check weekly off
                    w_days = policy.get('weekly_off_days')
                    if w_days is not None and isinstance(w_days, list):
                        is_weekly_off = w_idx in [int(x) for x in w_days]
                    else:
                        is_weekly_off = (w_idx == int(policy.get('weekly_off', 6)))

                    if is_on_leave:
                        status_val = 'ON_LEAVE'
                    elif is_holiday:
                        status_val = 'HOLIDAY'
                    elif is_weekly_off:
                        status_val = 'WEEKLY_OFF'
                    elif cal_date > today:
                        status_val = 'FUTURE'
                    elif cal_date < today:
                        # Prior date without punch is NOT_MARKED or ABSENT based on organization rule
                        status_val = 'NOT_MARKED'
                    else:
                        status_val = 'NOT_MARKED'

                    work_sec = 0
                    ot_sec = 0
                    emp_days_data[d_num] = {
                        'status': status_val,
                        'record_id': None,
                        'work_hours': '—',
                        'ot_hours': '—',
                        'method': '—',
                        'location_verified': False,
                        'is_overridden': False,
                    }

                # Update employee monthly counts
                key = status_val.lower()
                if key in emp_summary:
                    emp_summary[key] += 1
                emp_summary['total_seconds'] += work_sec
                emp_summary['ot_seconds'] += ot_sec

                # Update overall summary
                if key in overall_summary:
                    overall_summary[key] += 1
                overall_summary['total_hours'] += work_sec
                overall_summary['total_ot_hours'] += ot_sec

            emp_rows.append({
                'employee_id': str(emp.id),
                'employee_code': emp.employee_id,
                'name': emp.full_name,
                'centre_name': centre.name if centre else '—',
                'department': emp.department.name if emp.department else '—',
                'days': emp_days_data,
                'summary': {
                    **emp_summary,
                    'total_hours': round(emp_summary['total_seconds'] / 3600, 1),
                    'total_ot_hours': round(emp_summary['ot_seconds'] / 3600, 1),
                }
            })

        overall_summary['total_hours'] = round(overall_summary['total_hours'] / 3600, 1)
        overall_summary['total_ot_hours'] = round(overall_summary['total_ot_hours'] / 3600, 1)

        return Response({
            'year': year,
            'month': month,
            'num_days': num_days,
            'days_header': days_header,
            'employees': emp_rows,
            'summary': overall_summary,
        })


