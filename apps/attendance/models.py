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
    LEAVE = 'LEAVE', _('On Leave')
    HOLIDAY = 'HOLIDAY', _('Company Holiday')
    WEEK_OFF = 'WEEK_OFF', _('Weekly Off')


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
