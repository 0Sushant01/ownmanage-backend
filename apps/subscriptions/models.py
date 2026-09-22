from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedUUIDModel


class SubscriptionStatus(models.TextChoices):
    ACTIVE = 'ACTIVE', _('Active')
    TRIAL = 'TRIAL', _('Trial')
    EXPIRED = 'EXPIRED', _('Expired')
    CANCELLED = 'CANCELLED', _('Cancelled')


class SubscriptionAction(models.TextChoices):
    INITIAL = 'INITIAL', _('Initial Subscription')
    UPGRADE = 'UPGRADE', _('Plan Upgrade')
    DOWNGRADE = 'DOWNGRADE', _('Plan Downgrade')
    RENEWAL = 'RENEWAL', _('Plan Renewal')
    CANCEL = 'CANCEL', _('Cancellation')


class CommissionStatus(models.TextChoices):
    PENDING = 'PENDING', _('Pending')
    APPROVED = 'APPROVED', _('Approved')
    PAID = 'PAID', _('Paid')
    CANCELLED = 'CANCELLED', _('Cancelled')


class Broker(TimeStampedUUIDModel):
    """
    Platform-level partner role for referring enterprises and earning commissions.
    Completely isolated from enterprise operations.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='broker_profile',
        verbose_name=_('User Account')
    )
    name = models.CharField(max_length=255, verbose_name=_('Broker Name / Entity'))
    referral_code = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
        verbose_name=_('Referral Code'),
        help_text=_('Unique code given to enterprises during onboarding.')
    )
    commission_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=10.00,
        verbose_name=_('Commission Rate (%)'),
        help_text=_('Configurable commission percentage for referred subscription revenue.')
    )
    is_active = models.BooleanField(default=True, verbose_name=_('Is Active'))

    class Meta:
        verbose_name = _('Broker')
        verbose_name_plural = _('Brokers')
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.referral_code})"


class Referral(TimeStampedUUIDModel):
    """
    Permanent association between a Broker and a referred Enterprise.
    Stored permanently so changing today's UI or broker does not break historical provenance.
    """
    broker = models.ForeignKey(
        Broker,
        on_delete=models.PROTECT,
        related_name='referrals',
        verbose_name=_('Broker')
    )
    business = models.OneToOneField(
        'organization.Business',
        on_delete=models.PROTECT,
        related_name='referral',
        verbose_name=_('Referred Enterprise')
    )
    referral_code_used = models.CharField(max_length=50, verbose_name=_('Referral Code Used'))
    referred_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Referred Timestamp'))

    class Meta:
        verbose_name = _('Referral')
        verbose_name_plural = _('Referrals')
        ordering = ['-referred_at']

    def __str__(self):
        return f"{self.business} referred by {self.broker}"


class Plan(TimeStampedUUIDModel):
    """
    Commercial subscription plan configured by SuperAdmin.
    Defines monthly pricing, centre limits, total employee capacity, and feature entitlements.
    """
    name = models.CharField(max_length=100, unique=True, verbose_name=_('Plan Name'))
    monthly_charge = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name=_('Monthly Charge')
    )
    max_centres = models.PositiveIntegerField(
        default=5,
        verbose_name=_('Maximum Centres'),
        help_text=_('Upper limit of centres the enterprise can operate simultaneously.')
    )
    total_employee_capacity = models.PositiveIntegerField(
        default=100,
        verbose_name=_('Total Employee Capacity'),
        help_text=_('Total pool of employee seats across all centres.')
    )
    features = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_('Feature Entitlements'),
        help_text=_('Key-value feature flags such as attendance, leave, advanced_payroll, reports.')
    )
    is_active = models.BooleanField(default=True, verbose_name=_('Is Active'))

    class Meta:
        verbose_name = _('Subscription Plan')
        verbose_name_plural = _('Subscription Plans')
        ordering = ['monthly_charge', 'name']

    def __str__(self):
        return f"{self.name} (Max {self.max_centres} centres, {self.total_employee_capacity} capacity)"


class Subscription(TimeStampedUUIDModel):
    """
    Active subscription linking an Enterprise to a Plan.
    """
    business = models.OneToOneField(
        'organization.Business',
        on_delete=models.PROTECT,
        related_name='subscription',
        verbose_name=_('Enterprise')
    )
    plan = models.ForeignKey(
        Plan,
        on_delete=models.PROTECT,
        related_name='subscriptions',
        verbose_name=_('Current Plan')
    )
    status = models.CharField(
        max_length=30,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.ACTIVE,
        verbose_name=_('Status')
    )
    start_date = models.DateField(verbose_name=_('Subscription Start Date'))
    end_date = models.DateField(null=True, blank=True, verbose_name=_('Subscription End Date'))
    current_period_start = models.DateField(verbose_name=_('Period Start Date'))
    current_period_end = models.DateField(verbose_name=_('Period End Date'))

    class Meta:
        verbose_name = _('Subscription')
        verbose_name_plural = _('Subscriptions')

    def __str__(self):
        return f"{self.business} - {self.plan.name} ({self.status})"

    @property
    def total_allocated_capacity(self) -> int:
        return sum(a.allocated_capacity for a in self.centre_allocations.all())

    @property
    def unallocated_capacity(self) -> int:
        return max(0, self.plan.total_employee_capacity - self.total_allocated_capacity)


class SubscriptionHistory(TimeStampedUUIDModel):
    """
    Permanent audit log of subscription changes (upgrades, downgrades, renewals, cancellations).
    Preserves exact pricing and capacity for billing, audit, and historical commissions.
    """
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.PROTECT,
        related_name='subscription_history',
        verbose_name=_('Enterprise')
    )
    plan = models.ForeignKey(
        Plan,
        on_delete=models.PROTECT,
        verbose_name=_('Plan')
    )
    action = models.CharField(
        max_length=30,
        choices=SubscriptionAction.choices,
        verbose_name=_('Action')
    )
    monthly_charge = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_('Monthly Charge'))
    max_centres = models.PositiveIntegerField(verbose_name=_('Max Centres at Period'))
    total_employee_capacity = models.PositiveIntegerField(verbose_name=_('Capacity at Period'))
    effective_from = models.DateField(verbose_name=_('Effective From Date'))
    effective_to = models.DateField(null=True, blank=True, verbose_name=_('Effective To Date'))
    reason = models.TextField(blank=True, verbose_name=_('Change Reason'))

    class Meta:
        verbose_name = _('Subscription History')
        verbose_name_plural = _('Subscription History Records')
        ordering = ['-effective_from']

    def __str__(self):
        return f"{self.business} {self.action} to {self.plan.name} ({self.effective_from})"


class CentreCapacityAllocation(TimeStampedUUIDModel):
    """
    Individually allocated employee capacity per Centre.
    Enforces that sum of centre capacities <= Subscription plan capacity.
    """
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.CASCADE,
        related_name='centre_allocations',
        verbose_name=_('Subscription')
    )
    centre = models.OneToOneField(
        'organization.Branch',
        on_delete=models.CASCADE,
        related_name='capacity_allocation',
        verbose_name=_('Centre / Branch')
    )
    allocated_capacity = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Allocated Capacity')
    )

    class Meta:
        verbose_name = _('Centre Capacity Allocation')
        verbose_name_plural = _('Centre Capacity Allocations')
        indexes = [
            models.Index(fields=['subscription', 'centre'], name='idx_centre_cap_sub'),
        ]

    def __str__(self):
        return f"{self.centre.name}: {self.allocated_capacity} employees"


class Commission(TimeStampedUUIDModel):
    """
    Individual, immutable commission records for brokers per billing cycle.
    """
    broker = models.ForeignKey(
        Broker,
        on_delete=models.PROTECT,
        related_name='commissions',
        verbose_name=_('Broker')
    )
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.PROTECT,
        related_name='commissions',
        verbose_name=_('Enterprise')
    )
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='commissions',
        verbose_name=_('Subscription')
    )
    plan = models.ForeignKey(
        Plan,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_('Plan Billed')
    )
    period_start = models.DateField(verbose_name=_('Period Start'))
    period_end = models.DateField(verbose_name=_('Period End'))
    commission_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name=_('Commission Amount')
    )
    status = models.CharField(
        max_length=30,
        choices=CommissionStatus.choices,
        default=CommissionStatus.PENDING,
        verbose_name=_('Status')
    )
    paid_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Paid Timestamp'))
    payment_reference = models.CharField(max_length=100, blank=True, verbose_name=_('Payment Reference'))
    notes = models.TextField(blank=True, verbose_name=_('Notes'))

    class Meta:
        verbose_name = _('Commission Record')
        verbose_name_plural = _('Commission Records')
        ordering = ['-period_start']
        indexes = [
            models.Index(fields=['broker', 'status'], name='idx_comm_broker_status'),
            models.Index(fields=['business', '-period_start'], name='idx_comm_biz_period'),
        ]

    def __str__(self):
        return f"{self.broker.name} - {self.business.name}: {self.commission_amount} ({self.status})"
