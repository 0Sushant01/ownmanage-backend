from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedUUIDModel


class AttendanceStatus(models.TextChoices):
    """
    Standardized daily attendance status choices.
    """
    PRESENT = 'PRESENT', _('Present')
    ABSENT = 'ABSENT', _('Absent')
    HALF_DAY = 'HALF_DAY', _('Half Day')
    LATE = 'LATE', _('Late')
    LEAVE_EARLY = 'LEAVE_EARLY', _('Leave Early')
    OVERTIME = 'OVERTIME', _('Overtime')
    LEAVE = 'LEAVE', _('On Leave')
    HOLIDAY = 'HOLIDAY', _('Company Holiday')
    WEEK_OFF = 'WEEK_OFF', _('Weekly Off')
    NOT_MARKED = 'NOT_MARKED', _('Not Marked')


class AttendanceEventType(models.TextChoices):
    """
    Punch event types supporting multi-punch check-in/out and breaks.
    """
    CHECK_IN = 'CHECK_IN', _('Check In')
    CHECK_OUT = 'CHECK_OUT', _('Check Out')
    BREAK_START = 'BREAK_START', _('Break Start')
    BREAK_END = 'BREAK_END', _('Break End')


class AttendanceEventSource(models.TextChoices):
    """
    Input channel of attendance record.
    """
    WEB = 'WEB', _('Web Dashboard')
    MOBILE = 'MOBILE', _('Mobile App')
    BIOMETRIC = 'BIOMETRIC', _('Biometric Device')
    MANUAL_ADMIN = 'MANUAL_ADMIN', _('Manual Admin Entry')


class AttendanceDay(TimeStampedUUIDModel):
    """
    Daily attendance summary for an employee on a calendar date.
    Maintains denormalized totals while individual AttendanceEvents remain authoritative.
    """
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.PROTECT,
        related_name='attendance_days',
        verbose_name=_('Business')
    )
    employee = models.ForeignKey(
        'organization.Employee',
        on_delete=models.PROTECT,
        related_name='attendance_days',
        verbose_name=_('Employee')
    )
    centre = models.ForeignKey(
        'organization.Branch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='attendance_days',
        verbose_name=_('Centre / Branch')
    )
    attendance_date = models.DateField(verbose_name=_('Attendance Date'))
    status = models.CharField(
        max_length=30,
        choices=AttendanceStatus.choices,
        default=AttendanceStatus.PRESENT,
        verbose_name=_('Status')
    )
    total_work_seconds = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Total Work Seconds'),
        help_text=_('Aggregated active work duration in seconds derived from punch pairs.')
    )
    overtime_seconds = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Overtime Seconds')
    )
    late_minutes = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Late Arrival Minutes')
    )
    early_leave_minutes = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Early Departure Minutes')
    )
    is_locked = models.BooleanField(
        default=False,
        verbose_name=_('Is Locked'),
        help_text=_('Locks the day from further check-in edits once payroll for this period is approved.')
    )
    notes = models.TextField(blank=True, verbose_name=_('Notes'))

    class Meta:
        verbose_name = _('Attendance Day')
        verbose_name_plural = _('Attendance Days')
        ordering = ['-attendance_date', 'employee']
        constraints = [
            models.UniqueConstraint(
                fields=['employee', 'attendance_date'],
                name='unique_employee_attendance_date'
            )
        ]
        indexes = [
            models.Index(fields=['business', '-attendance_date'], name='idx_att_biz_date'),
            models.Index(fields=['employee', '-attendance_date'], name='idx_att_emp_date'),
            models.Index(fields=['centre', '-attendance_date'], name='idx_att_centre_date'),
            models.Index(fields=['business', 'status'], name='idx_att_biz_status'),
        ]

    def __str__(self):
        return f"{self.employee} - {self.attendance_date} ({self.get_status_display()})"


class AttendanceEvent(TimeStampedUUIDModel):
    """
    Authoritative, immutable raw punch event (Check-in, Check-out, Break start/end).
    Captures exact timestamp, geolocation, and device telemetry.
    """
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.PROTECT,
        related_name='attendance_events',
        verbose_name=_('Business')
    )
    attendance_day = models.ForeignKey(
        AttendanceDay,
        on_delete=models.CASCADE,
        related_name='events',
        verbose_name=_('Attendance Day')
    )
    employee = models.ForeignKey(
        'organization.Employee',
        on_delete=models.PROTECT,
        related_name='attendance_events',
        verbose_name=_('Employee')
    )
    event_type = models.CharField(
        max_length=30,
        choices=AttendanceEventType.choices,
        verbose_name=_('Event Type')
    )
    event_time = models.DateTimeField(
        db_index=True,
        verbose_name=_('Event Timestamp (UTC)')
    )
    latitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        verbose_name=_('Latitude')
    )
    longitude = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        verbose_name=_('Longitude')
    )
    location_accuracy = models.FloatField(
        null=True,
        blank=True,
        verbose_name=_('Location Accuracy (Meters)')
    )
    device_id = models.CharField(
        max_length=150,
        blank=True,
        verbose_name=_('Device ID / Identifier')
    )
    source = models.CharField(
        max_length=30,
        choices=AttendanceEventSource.choices,
        default=AttendanceEventSource.WEB,
        verbose_name=_('Source Channel')
    )
    notes = models.TextField(blank=True, verbose_name=_('Event Notes'))

    class Meta:
        verbose_name = _('Attendance Event')
        verbose_name_plural = _('Attendance Events')
        ordering = ['event_time']
        indexes = [
            models.Index(fields=['attendance_day', 'event_time'], name='idx_attevt_day_time'),
            models.Index(fields=['employee', '-event_time'], name='idx_attevt_emp_time'),
            models.Index(fields=['business', '-event_time'], name='idx_attevt_biz_time'),
        ]

    def __str__(self):
        return f"{self.employee} {self.get_event_type_display()} at {self.event_time}"


class WorkSchedule(TimeStampedUUIDModel):
    """
    Reusable organizational shift and work schedule definitions.
    Supports standard shifts, overnight shifts, configurable grace periods, and business defaults.
    """
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.CASCADE,
        related_name='schedules',
        verbose_name=_('Business')
    )
    name = models.CharField(max_length=100, verbose_name=_('Schedule Name'))
    start_time = models.TimeField(default='09:00:00', verbose_name=_('Shift Start Time'))
    end_time = models.TimeField(default='18:00:00', verbose_name=_('Shift End Time'))
    is_overnight = models.BooleanField(
        default=False,
        verbose_name=_('Is Overnight Shift'),
        help_text=_('Designates whether the shift crosses midnight (e.g. 22:00 to 06:00).')
    )
    grace_period_minutes = models.PositiveIntegerField(
        default=15,
        verbose_name=_('Grace Period (Minutes)'),
        help_text=_('Configurable tolerance window before check-in is flagged as LATE.')
    )
    is_business_default = models.BooleanField(
        default=False,
        verbose_name=_('Is Default Schedule')
    )
    is_active = models.BooleanField(default=True, verbose_name=_('Is Active'))

    class Meta:
        verbose_name = _('Work Schedule')
        verbose_name_plural = _('Work Schedules')
        ordering = ['business', 'name']

    def __str__(self):
        overnight = " (Overnight)" if self.is_overnight else ""
        return f"{self.name} [{self.start_time.strftime('%H:%M')} - {self.end_time.strftime('%H:%M')}]{overnight}"


class WorkScheduleDay(TimeStampedUUIDModel):
    """
    Day-of-week rules for a work schedule (e.g. Monday-Friday active, Sat/Sun weekly off).
    """
    DAYS_OF_WEEK = [
        (0, _('Monday')),
        (1, _('Tuesday')),
        (2, _('Wednesday')),
        (3, _('Thursday')),
        (4, _('Friday')),
        (5, _('Saturday')),
        (6, _('Sunday')),
    ]
    schedule = models.ForeignKey(
        WorkSchedule,
        on_delete=models.CASCADE,
        related_name='days',
        verbose_name=_('Schedule')
    )
    day_of_week = models.PositiveSmallIntegerField(
        choices=DAYS_OF_WEEK,
        verbose_name=_('Day of Week')
    )
    is_work_day = models.BooleanField(default=True, verbose_name=_('Is Work Day'))
    start_time = models.TimeField(null=True, blank=True, verbose_name=_('Start Time Override'))
    end_time = models.TimeField(null=True, blank=True, verbose_name=_('End Time Override'))

    class Meta:
        verbose_name = _('Schedule Day Rule')
        verbose_name_plural = _('Schedule Day Rules')
        constraints = [
            models.UniqueConstraint(
                fields=['schedule', 'day_of_week'],
                name='unique_schedule_day_of_week'
            )
        ]
        ordering = ['schedule', 'day_of_week']

    def __str__(self):
        return f"{self.schedule.name} - {self.get_day_of_week_display()}: {'Work' if self.is_work_day else 'Off'}"


class EmployeeScheduleAssignment(TimeStampedUUIDModel):
    """
    Effective-dated schedule assignment per employee.
    Preserves historical schedule context when shifts or shift timings change.
    """
    employee = models.ForeignKey(
        'organization.Employee',
        on_delete=models.CASCADE,
        related_name='schedule_assignments',
        verbose_name=_('Employee')
    )
    schedule = models.ForeignKey(
        WorkSchedule,
        on_delete=models.PROTECT,
        related_name='assignments',
        verbose_name=_('Work Schedule')
    )
    effective_from = models.DateField(verbose_name=_('Effective From Date'))
    effective_to = models.DateField(null=True, blank=True, verbose_name=_('Effective To Date'))

    class Meta:
        verbose_name = _('Employee Schedule Assignment')
        verbose_name_plural = _('Employee Schedule Assignments')
        ordering = ['-effective_from']
        indexes = [
            models.Index(fields=['employee', '-effective_from'], name='idx_empsch_eff_from'),
        ]

    def __str__(self):
        return f"{self.employee}: {self.schedule.name} ({self.effective_from} to {self.effective_to or 'Present'})"


class AttendanceCorrection(TimeStampedUUIDModel):
    """
    Formal correction request for missing/incorrect punch events.
    Preserves original event values and approver decision audit trail without overwriting history.
    """
    CORRECTION_TYPES = [
        ('CHECK_IN', _('Check In')),
        ('CHECK_OUT', _('Check Out')),
        ('STATUS', _('Daily Status')),
    ]
    CORRECTION_STATUS = [
        ('PENDING', _('Pending Approval')),
        ('APPROVED', _('Approved')),
        ('REJECTED', _('Rejected')),
    ]
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.PROTECT,
        related_name='attendance_corrections',
        verbose_name=_('Business')
    )
    attendance_day = models.ForeignKey(
        AttendanceDay,
        on_delete=models.CASCADE,
        related_name='corrections',
        verbose_name=_('Attendance Day')
    )
    original_event = models.ForeignKey(
        AttendanceEvent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='corrections',
        verbose_name=_('Original Event')
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='requested_attendance_corrections',
        verbose_name=_('Requested By')
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_attendance_corrections',
        verbose_name=_('Reviewed By')
    )
    correction_type = models.CharField(
        max_length=30,
        choices=CORRECTION_TYPES,
        verbose_name=_('Correction Type')
    )
    corrected_time = models.DateTimeField(null=True, blank=True, verbose_name=_('Corrected Timestamp'))
    corrected_status = models.CharField(
        max_length=30,
        blank=True,
        choices=AttendanceStatus.choices,
        verbose_name=_('Corrected Status')
    )
    reason = models.TextField(verbose_name=_('Reason for Correction'))
    status = models.CharField(
        max_length=30,
        choices=CORRECTION_STATUS,
        default='PENDING',
        verbose_name=_('Status')
    )
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Reviewed Timestamp'))
    review_notes = models.TextField(blank=True, verbose_name=_('Review Notes'))

    class Meta:
        verbose_name = _('Attendance Correction')
        verbose_name_plural = _('Attendance Corrections')
        ordering = ['-created_at']

    def __str__(self):
        return f"Correction for {self.attendance_day}: {self.correction_type} ({self.status})"


class AttendancePolicy(TimeStampedUUIDModel):
    """
    Enterprise-level attendance policy defining global defaults for all centres.
    """
    business = models.OneToOneField(
        'organization.Business',
        on_delete=models.CASCADE,
        related_name='attendance_policy',
        verbose_name=_('Business')
    )
    # Work Schedule Defaults
    office_start = models.TimeField(default='09:00:00', verbose_name=_('Office Start Time'))
    office_end = models.TimeField(default='18:00:00', verbose_name=_('Office End Time'))
    working_days = models.PositiveSmallIntegerField(default=5, verbose_name=_('Working Days per Week'))
    weekly_off = models.PositiveSmallIntegerField(
        default=6,
        verbose_name=_('Weekly Off Day'),
        help_text=_('Legacy single weekly off: 0=Monday, 6=Sunday')
    )
    weekly_off_days = models.JSONField(
        default=list,
        blank=True,
        verbose_name=_('Weekly Off Days'),
        help_text=_('List of weekly off days [0..6] where 0=Monday, 6=Sunday. Allows 0 to 7 days.')
    )
    daily_schedules = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_('Daily Schedules'),
        help_text=_('Per-day working hours map, e.g. {"0": {"start": "09:00", "end": "18:00"}}')
    )
    break_start = models.TimeField(null=True, blank=True, verbose_name=_('Break Start Time'))
    break_end = models.TimeField(null=True, blank=True, verbose_name=_('Break End Time'))

    # Attendance Rules
    grace_period_minutes = models.PositiveIntegerField(
        default=15,
        verbose_name=_('Grace Period (Minutes)'),
        help_text=_('Window after shift start where check-in is still considered PRESENT without late penalty.')
    )
    minimum_present_minutes = models.PositiveIntegerField(
        default=480,
        verbose_name=_('Minimum Present Minutes'),
        help_text=_('Minimum active work time required for a full day PRESENT status (e.g. 480 min = 8 hours).')
    )
    minimum_half_day_minutes = models.PositiveIntegerField(
        default=240,
        verbose_name=_('Minimum Half Day Minutes'),
        help_text=_('Minimum active work time required for HALF_DAY status (e.g. 240 min = 4 hours).')
    )
    late_threshold_minutes = models.PositiveIntegerField(default=0, verbose_name=_('Late Threshold (Minutes)'))
    early_checkout_threshold_minutes = models.PositiveIntegerField(default=0, verbose_name=_('Early Checkout Threshold (Minutes)'))
    auto_attendance = models.BooleanField(
        default=True,
        verbose_name=_('Auto Attendance Enabled'),
        help_text=_('Automatically calculate daily attendance status from punch events.')
    )
    allow_center_override = models.BooleanField(
        default=True,
        verbose_name=_('Allow Centre Overrides'),
        help_text=_('Whether permitted managers can override specific rules for their assigned centre.')
    )

    # Overtime Rules
    ot_enabled = models.BooleanField(default=False, verbose_name=_('Overtime Enabled'))
    ot_grace_minutes = models.PositiveIntegerField(default=30, verbose_name=_('Overtime Grace Minutes'))
    ot_approval_required = models.BooleanField(default=True, verbose_name=_('Overtime Approval Required'))
    max_daily_ot_minutes = models.PositiveIntegerField(default=240, verbose_name=_('Maximum Daily Overtime Minutes'))

    # Attendance Capture Modes (Feature Flags)
    allow_normal_punch = models.BooleanField(default=True, verbose_name=_('Allow Normal Check-In'))
    allow_gps = models.BooleanField(default=True, verbose_name=_('Allow GPS Check-In'))
    allow_geofencing = models.BooleanField(default=False, verbose_name=_('Allow Geofencing'))
    allow_qr = models.BooleanField(default=False, verbose_name=_('Allow QR Code'))
    allow_face_recognition = models.BooleanField(default=False, verbose_name=_('Allow Face Recognition'))
    allow_biometric = models.BooleanField(default=False, verbose_name=_('Allow Biometric Device'))

    # GPS and Location defaults
    gps_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, verbose_name=_('GPS Latitude'))
    gps_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, verbose_name=_('GPS Longitude'))
    gps_radius_meters = models.PositiveIntegerField(default=100, verbose_name=_('GPS Allowed Radius (meters)'))
    location_required_checkin = models.BooleanField(default=False, verbose_name=_('Location Required Check-in'))
    location_required_checkout = models.BooleanField(default=False, verbose_name=_('Location Required Check-out'))

    extra_settings = models.JSONField(default=dict, blank=True, verbose_name=_('Extensible Metadata'))

    class Meta:
        verbose_name = _('Attendance Policy')
        verbose_name_plural = _('Attendance Policies')

    def __str__(self):
        return f"Attendance Policy ({self.business.name})"


class AttendancePolicyOverride(TimeStampedUUIDModel):
    """
    Centre-level override of Enterprise Attendance Policy.
    Stores only the actual overrides. Null fields inherit the Enterprise default.
    """
    centre = models.OneToOneField(
        'organization.Branch',
        on_delete=models.CASCADE,
        related_name='attendance_policy_override',
        verbose_name=_('Centre / Branch')
    )

    # Overridable fields (Nullable: if None, inherits Enterprise default)
    office_start = models.TimeField(null=True, blank=True, verbose_name=_('Office Start Time Override'))
    office_end = models.TimeField(null=True, blank=True, verbose_name=_('Office End Time Override'))
    grace_period_minutes = models.PositiveIntegerField(null=True, blank=True, verbose_name=_('Grace Period Override'))
    minimum_present_minutes = models.PositiveIntegerField(null=True, blank=True, verbose_name=_('Minimum Present Override'))
    minimum_half_day_minutes = models.PositiveIntegerField(null=True, blank=True, verbose_name=_('Minimum Half-Day Override'))
    weekly_off = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name=_('Weekly Off Override'))
    weekly_off_days = models.JSONField(null=True, blank=True, verbose_name=_('Weekly Off Days Override'))
    daily_schedules = models.JSONField(null=True, blank=True, verbose_name=_('Daily Schedules Override'))
    break_start = models.TimeField(null=True, blank=True, verbose_name=_('Break Start Override'))
    break_end = models.TimeField(null=True, blank=True, verbose_name=_('Break End Override'))
    late_threshold_minutes = models.PositiveIntegerField(null=True, blank=True, verbose_name=_('Late Threshold Override'))
    early_checkout_threshold_minutes = models.PositiveIntegerField(null=True, blank=True, verbose_name=_('Early Checkout Threshold Override'))
    auto_attendance = models.BooleanField(null=True, blank=True, verbose_name=_('Auto Attendance Override'))

    # Overtime Overrides
    ot_enabled = models.BooleanField(null=True, blank=True, verbose_name=_('Overtime Enabled Override'))
    ot_grace_minutes = models.PositiveIntegerField(null=True, blank=True, verbose_name=_('OT Grace Override'))

    # Verification Method Overrides
    allow_normal_punch = models.BooleanField(null=True, blank=True, verbose_name=_('Allow Normal Punch Override'))
    allow_gps = models.BooleanField(null=True, blank=True, verbose_name=_('Allow GPS Override'))
    allow_geofencing = models.BooleanField(null=True, blank=True, verbose_name=_('Allow Geofencing Override'))
    allow_qr = models.BooleanField(null=True, blank=True, verbose_name=_('Allow QR Override'))
    allow_face_recognition = models.BooleanField(null=True, blank=True, verbose_name=_('Allow Face Override'))
    allow_biometric = models.BooleanField(null=True, blank=True, verbose_name=_('Allow Biometric Override'))

    # GPS Overrides
    gps_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, verbose_name=_('GPS Latitude Override'))
    gps_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, verbose_name=_('GPS Longitude Override'))
    gps_radius_meters = models.PositiveIntegerField(null=True, blank=True, verbose_name=_('GPS Radius Override'))
    location_required_checkin = models.BooleanField(null=True, blank=True, verbose_name=_('Location Required Check-in Override'))
    location_required_checkout = models.BooleanField(null=True, blank=True, verbose_name=_('Location Required Check-out Override'))

    extra_settings = models.JSONField(default=dict, blank=True, verbose_name=_('Custom Centre Settings'))

    class Meta:
        verbose_name = _('Attendance Policy Override')
        verbose_name_plural = _('Attendance Policy Overrides')

    def __str__(self):
        return f"Policy Override for {self.centre.name}"
