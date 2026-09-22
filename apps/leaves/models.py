from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedUUIDModel


class LeaveRequestStatus(models.TextChoices):
    """
    Approval workflow statuses for leave requests.
    """
    PENDING = 'PENDING', _('Pending Approval')
    APPROVED = 'APPROVED', _('Approved')
    REJECTED = 'REJECTED', _('Rejected')
    CANCELLED = 'CANCELLED', _('Cancelled')


class LeaveDuration(models.TextChoices):
    FULL_DAY = 'FULL_DAY', _('Full Day')
    FIRST_HALF = 'FIRST_HALF', _('First Half')
    SECOND_HALF = 'SECOND_HALF', _('Second Half')


class LeaveType(TimeStampedUUIDModel):
    """
    Business-specific leave category (e.g. Sick Leave, Casual Leave, Earned Leave).
    """
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.PROTECT,
        related_name='leave_types',
        verbose_name=_('Business')
    )
    name = models.CharField(max_length=100, verbose_name=_('Leave Type Name'))
    code = models.CharField(max_length=30, verbose_name=_('Leave Code'))
    description = models.TextField(blank=True, verbose_name=_('Description'))
    is_paid = models.BooleanField(default=True, verbose_name=_('Is Paid Leave'))
    is_active = models.BooleanField(default=True, verbose_name=_('Is Active'))

    class Meta:
        verbose_name = _('Leave Type')
        verbose_name_plural = _('Leave Types')
        ordering = ['business', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['business', 'code'],
                name='unique_business_leave_type_code'
            ),
            models.UniqueConstraint(
                fields=['business', 'name'],
                name='unique_business_leave_type_name'
            ),
        ]
        indexes = [
            models.Index(fields=['business', 'is_active'], name='idx_leavetype_biz_active'),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"


class LeaveRequest(TimeStampedUUIDModel):
    """
    Formal leave application submitted by an employee.
    Retains immutable approver audit trail and decision timestamps.
    """
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.PROTECT,
        related_name='leave_requests',
        verbose_name=_('Business')
    )
    employee = models.ForeignKey(
        'organization.Employee',
        on_delete=models.PROTECT,
        related_name='leave_requests',
        verbose_name=_('Employee')
    )
    leave_type = models.ForeignKey(
        LeaveType,
        on_delete=models.PROTECT,
        related_name='leave_requests',
        verbose_name=_('Leave Type')
    )
    start_date = models.DateField(verbose_name=_('Start Date'))
    end_date = models.DateField(verbose_name=_('End Date'))
    duration_type = models.CharField(
        max_length=20,
        choices=LeaveDuration.choices,
        default=LeaveDuration.FULL_DAY,
        verbose_name=_('Leave Duration')
    )
    reason = models.TextField(verbose_name=_('Reason for Leave'))
    status = models.CharField(
        max_length=30,
        choices=LeaveRequestStatus.choices,
        default=LeaveRequestStatus.PENDING,
        verbose_name=_('Status')
    )

    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_leaves',
        verbose_name=_('Approved / Rejected By')
    )
    approved_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Approved At'))
    rejected_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Rejected At'))
    rejection_reason = models.TextField(blank=True, verbose_name=_('Rejection Reason'))

    class Meta:
        verbose_name = _('Leave Request')
        verbose_name_plural = _('Leave Requests')
        ordering = ['-start_date']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__gte=models.F('start_date')),
                name='check_leave_dates_valid'
            ),
        ]
        indexes = [
            models.Index(fields=['business', 'status'], name='idx_leavereq_biz_status'),
            models.Index(fields=['employee', '-start_date'], name='idx_leavereq_emp_start'),
            models.Index(fields=['business', '-start_date'], name='idx_leavereq_biz_start'),
        ]

    def __str__(self):
        return f"{self.employee}: {self.leave_type.code} ({self.start_date} to {self.end_date}) [{self.status}]"


class LeavePolicy(TimeStampedUUIDModel):
    """
    Policy configuring annual entitlement and carry-forward rules per leave type.
    """
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.CASCADE,
        related_name='leave_policies',
        verbose_name=_('Business')
    )
    leave_type = models.ForeignKey(
        LeaveType,
        on_delete=models.CASCADE,
        related_name='policies',
        verbose_name=_('Leave Type')
    )
    annual_days = models.PositiveIntegerField(
        default=12,
        verbose_name=_('Annual Entitlement (Days)')
    )
    carry_forward_days = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Max Carry Forward Days')
    )
    effective_from = models.DateField(verbose_name=_('Effective From Date'))
    effective_to = models.DateField(null=True, blank=True, verbose_name=_('Effective To Date'))

    class Meta:
        verbose_name = _('Leave Policy')
        verbose_name_plural = _('Leave Policies')
        ordering = ['business', '-effective_from']

    def __str__(self):
        return f"{self.leave_type.name} Policy ({self.annual_days} days/year)"


class LeaveBalance(TimeStampedUUIDModel):
    """
    Tracks annual quota, utilized days, and pending deductions per employee.
    """
    employee = models.ForeignKey(
        'organization.Employee',
        on_delete=models.CASCADE,
        related_name='leave_balances',
        verbose_name=_('Employee')
    )
    leave_type = models.ForeignKey(
        LeaveType,
        on_delete=models.PROTECT,
        related_name='balances',
        verbose_name=_('Leave Type')
    )
    year = models.PositiveIntegerField(verbose_name=_('Calendar Year'))
    allocated_days = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        default=0.0,
        verbose_name=_('Allocated Days')
    )
    used_days = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        default=0.0,
        verbose_name=_('Used Days')
    )
    pending_days = models.DecimalField(
        max_digits=5,
        decimal_places=1,
        default=0.0,
        verbose_name=_('Pending Days')
    )

    class Meta:
        verbose_name = _('Leave Balance')
        verbose_name_plural = _('Leave Balances')
        constraints = [
            models.UniqueConstraint(
                fields=['employee', 'leave_type', 'year'],
                name='unique_emp_leave_year_balance'
            )
        ]
        ordering = ['employee', '-year']

    def __str__(self):
        available = self.allocated_days - self.used_days - self.pending_days
        return f"{self.employee} - {self.leave_type.code} ({self.year}): {available} days available"

