from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedUUIDModel


class CompensationType(models.TextChoices):
    MONTHLY_SALARY = 'MONTHLY_SALARY', _('Fixed Monthly Salary')
    DAILY_WAGE = 'DAILY_WAGE', _('Daily Wage')
    HOURLY_WAGE = 'HOURLY_WAGE', _('Hourly Wage')
    FIXED_CONTRACT = 'FIXED_CONTRACT', _('Fixed Contract Amount')


class PayFrequency(models.TextChoices):
    DAILY = 'DAILY', _('Daily')
    WEEKLY = 'WEEKLY', _('Weekly')
    FORTNIGHTLY = 'FORTNIGHTLY', _('Fortnightly')
    MONTHLY_CALENDAR = 'MONTHLY_CALENDAR', _('Monthly — Calendar')
    MONTHLY_CUSTOM = 'MONTHLY_CUSTOM', _('Monthly — Custom Cycle')
    CUSTOM_PERIOD = 'CUSTOM_PERIOD', _('Custom Period')


class MonthEndRule(models.TextChoices):
    CLAMP_TO_LAST_DAY = 'CLAMP_TO_LAST_DAY', _('Clamp to Last Day of Month')
    NEXT_AVAILABLE_DAY = 'NEXT_AVAILABLE_DAY', _('Next Available Day')


class PayrollGenerationMode(models.TextChoices):
    MANUAL = 'MANUAL', _('Manual Generation')
    AUTOMATIC_DRAFT_AFTER_PERIOD_END = 'AUTOMATIC_DRAFT_AFTER_PERIOD_END', _('Automatic Draft After Period End')
    AUTOMATIC_DRAFT_ON_CONFIGURED_DATE = 'AUTOMATIC_DRAFT_ON_CONFIGURED_DATE', _('Automatic Draft on Configured Date')
    AUTOMATIC_DRAFT_BEFORE_PERIOD_END = 'AUTOMATIC_DRAFT_BEFORE_PERIOD_END', _('Automatic Draft Before Period End')


class PaymentScheduleRule(models.TextChoices):
    SAME_DAY_AS_PERIOD_END = 'SAME_DAY_AS_PERIOD_END', _('Same Day as Period End')
    DAYS_AFTER_PERIOD_END = 'DAYS_AFTER_PERIOD_END', _('Fixed Days After Period End')
    SPECIFIED_WEEKDAY = 'SPECIFIED_WEEKDAY', _('Specified Weekday')
    DAY_OF_FOLLOWING_MONTH = 'DAY_OF_FOLLOWING_MONTH', _('Fixed Day of Following Month')
    CUSTOM_RULE = 'CUSTOM_RULE', _('Custom Payment Rule')
    MANUAL = 'MANUAL', _('Manual Selection')


class ScheduleConfigScope(models.TextChoices):
    ENTERPRISE = 'ENTERPRISE', _('Enterprise Default')
    CENTRE = 'CENTRE', _('Centre Override')
    EMPLOYEE = 'EMPLOYEE', _('Employee Override')


class PayrollStatus(models.TextChoices):
    """
    Lifecycle status for payroll calculation and disbursement.
    """
    DRAFT = 'DRAFT', _('Draft')
    PROCESSED = 'PROCESSED', _('Processed')
    PAID = 'PAID', _('Paid')
    CANCELLED = 'CANCELLED', _('Cancelled')


class SalaryComponentType(models.TextChoices):
    EARNING = 'EARNING', _('Earning')
    DEDUCTION = 'DEDUCTION', _('Deduction')


class CompensationComponentType(models.TextChoices):
    EARNING = 'EARNING', _('Earning')
    BONUS = 'BONUS', _('Bonus')
    ALLOWANCE = 'ALLOWANCE', _('Allowance')
    DEDUCTION = 'DEDUCTION', _('Deduction')
    OVERTIME = 'OVERTIME', _('Overtime')


class CompensationCalculationType(models.TextChoices):
    FIXED_AMOUNT = 'FIXED_AMOUNT', _('Fixed Amount')
    PERCENTAGE = 'PERCENTAGE', _('Percentage')


class CompensationFrequency(models.TextChoices):
    RECURRING = 'RECURRING', _('Recurring Monthly')
    ONE_TIME = 'ONE_TIME', _('One-Time')


class SalaryStructure(TimeStampedUUIDModel):
    """
    Historical salary definitions per employee.
    Preserves salary timeline so changes do not mutate historical payroll records.
    """
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.PROTECT,
        related_name='salary_structures',
        verbose_name=_('Business')
    )
    employee = models.ForeignKey(
        'organization.Employee',
        on_delete=models.PROTECT,
        related_name='salary_structures',
        verbose_name=_('Employee')
    )
    effective_from = models.DateField(verbose_name=_('Effective From Date'))
    effective_to = models.DateField(null=True, blank=True, verbose_name=_('Effective To Date'))
    basic_salary = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name=_('Basic Salary Amount'),
        help_text=_('Fixed basic salary component.')
    )
    currency = models.CharField(
        max_length=10,
        default='INR',
        verbose_name=_('Currency')
    )

    class Meta:
        verbose_name = _('Salary Structure')
        verbose_name_plural = _('Salary Structures')
        ordering = ['-effective_from']
        indexes = [
            models.Index(fields=['employee', '-effective_from'], name='idx_sal_emp_eff_from'),
            models.Index(fields=['business', '-effective_from'], name='idx_sal_biz_eff_from'),
        ]

    def __str__(self):
        return f"{self.employee}: {self.currency} {self.basic_salary} (from {self.effective_from})"


class SalaryComponent(TimeStampedUUIDModel):
    """
    Itemized salary components (e.g. Basic, HRA, Travel, Medical, PF Deduction, Tax).
    """
    structure = models.ForeignKey(
        SalaryStructure,
        on_delete=models.CASCADE,
        related_name='components',
        verbose_name=_('Salary Structure')
    )
    name = models.CharField(max_length=100, verbose_name=_('Component Name'))
    component_type = models.CharField(
        max_length=20,
        choices=SalaryComponentType.choices,
        default=SalaryComponentType.EARNING,
        verbose_name=_('Component Type')
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_('Amount'))

    class Meta:
        verbose_name = _('Salary Component')
        verbose_name_plural = _('Salary Components')

    def __str__(self):
        return f"{self.name} [{self.component_type}]: {self.amount}"


class EmployeeSalaryAssignment(TimeStampedUUIDModel):
    """
    Effective-dated assignment linking an employee to their applicable salary structure.
    """
    employee = models.ForeignKey(
        'organization.Employee',
        on_delete=models.CASCADE,
        related_name='salary_assignments',
        verbose_name=_('Employee')
    )
    structure = models.ForeignKey(
        SalaryStructure,
        on_delete=models.PROTECT,
        related_name='assignments',
        verbose_name=_('Salary Structure')
    )
    effective_from = models.DateField(verbose_name=_('Effective From Date'))
    effective_to = models.DateField(null=True, blank=True, verbose_name=_('Effective To Date'))

    class Meta:
        verbose_name = _('Employee Salary Assignment')
        verbose_name_plural = _('Employee Salary Assignments')
        ordering = ['-effective_from']

    def __str__(self):
        return f"{self.employee}: Structure {self.structure.id} ({self.effective_from} to {self.effective_to or 'Present'})"


class SalaryRevision(TimeStampedUUIDModel):
    """
    Permanent audit log of employee compensation changes.
    Preserves historical salary and component breakdowns for retroactive calculations.
    """
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.CASCADE,
        related_name='salary_revisions',
        verbose_name=_('Business')
    )
    employee = models.ForeignKey(
        'organization.Employee',
        on_delete=models.CASCADE,
        related_name='salary_revisions',
        verbose_name=_('Employee')
    )
    effective_from = models.DateField(verbose_name=_('Effective From Date'))
    effective_to = models.DateField(null=True, blank=True, verbose_name=_('Effective To Date'))
    basic_salary = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name=_('Base Monthly Salary')
    )
    hourly_rate = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0.00,
        verbose_name=_('Hourly Rate')
    )
    ot_rate = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0.00,
        verbose_name=_('Overtime Hourly Rate')
    )
    currency = models.CharField(
        max_length=10,
        default='INR',
        verbose_name=_('Currency')
    )
    allowances = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_('Allowances Breakdown'),
        help_text=_('Itemized allowances, e.g. {"hra": 5000, "conveyance": 2000, "medical": 1250}')
    )
    deduction_rules = models.JSONField(
        default=dict,
        blank=True,
        verbose_name=_('Deductions Breakdown'),
        help_text=_('Itemized deductions, e.g. {"pf": 1800, "tax": 1000, "esi": 350}')
    )
    reason = models.TextField(blank=True, verbose_name=_('Revision Reason'))
    revised_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='salary_revisions_authored',
        verbose_name=_('Revised By')
    )

    class Meta:
        verbose_name = _('Salary Revision')
        verbose_name_plural = _('Salary Revisions')
        ordering = ['-effective_from']
        indexes = [
            models.Index(fields=['employee', '-effective_from'], name='idx_salrev_emp_eff'),
            models.Index(fields=['business', '-effective_from'], name='idx_salrev_biz_eff'),
        ]

    def __str__(self):
        return f"{self.employee}: {self.currency} {self.basic_salary} (from {self.effective_from})"


class PayrollRunStatus(models.TextChoices):
    DRAFT = 'DRAFT', _('Draft')
    CALCULATING = 'CALCULATING', _('Calculating')
    REVIEW = 'REVIEW', _('In Review')
    APPROVED = 'APPROVED', _('Approved')
    FINALIZED = 'FINALIZED', _('Finalized')
    CANCELLED = 'CANCELLED', _('Cancelled')


class PayrollRun(TimeStampedUUIDModel):
    """
    Batched enterprise/centre payroll execution lifecycle.
    """
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.CASCADE,
        related_name='payroll_runs',
        verbose_name=_('Business')
    )
    centre = models.ForeignKey(
        'organization.Branch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payroll_runs',
        verbose_name=_('Centre / Branch')
    )
    period_start = models.DateField(verbose_name=_('Period Start Date'))
    period_end = models.DateField(verbose_name=_('Period End Date'))
    status = models.CharField(
        max_length=30,
        choices=PayrollRunStatus.choices,
        default=PayrollRunStatus.DRAFT,
        verbose_name=_('Run Status')
    )
    total_employees = models.PositiveIntegerField(default=0, verbose_name=_('Total Employees Processed'))
    total_gross = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0.00,
        verbose_name=_('Total Gross Amount')
    )
    total_deductions = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0.00,
        verbose_name=_('Total Deductions')
    )
    total_net = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0.00,
        verbose_name=_('Total Net Amount')
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_payroll_runs',
        verbose_name=_('Approved By')
    )
    finalized_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Finalized Timestamp'))
    expected_payment_date = models.DateField(
        null=True,
        blank=True,
        verbose_name=_('Expected Payment Date')
    )
    generation_mode = models.CharField(
        max_length=50,
        choices=PayrollGenerationMode.choices,
        default=PayrollGenerationMode.MANUAL,
        verbose_name=_('Generation Mode')
    )
    pay_frequency = models.CharField(
        max_length=50,
        choices=PayFrequency.choices,
        default=PayFrequency.MONTHLY_CALENDAR,
        verbose_name=_('Pay Frequency')
    )
    schedule_config = models.ForeignKey(
        'payroll.PayrollScheduleConfig',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payroll_runs',
        verbose_name=_('Schedule Configuration')
    )

    class Meta:
        verbose_name = _('Payroll Run')
        verbose_name_plural = _('Payroll Runs')
        ordering = ['-period_start', '-created_at']
        indexes = [
            models.Index(fields=['business', '-period_start'], name='idx_prun_biz_period'),
            models.Index(fields=['centre', '-period_start'], name='idx_prun_centre_period'),
        ]

    def __str__(self):
        return f"Payroll Run {self.period_start} to {self.period_end} ({self.get_status_display()})"


class Payroll(TimeStampedUUIDModel):
    """
    Frozen, immutable snapshot of employee remuneration for an arbitrary time period.
    """
    payroll_run = models.ForeignKey(
        PayrollRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payrolls',
        verbose_name=_('Batch Payroll Run')
    )
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.PROTECT,
        related_name='payrolls',
        verbose_name=_('Business')
    )
    employee = models.ForeignKey(
        'organization.Employee',
        on_delete=models.PROTECT,
        related_name='payrolls',
        verbose_name=_('Employee')
    )
    period_start = models.DateField(verbose_name=_('Period Start Date'))
    period_end = models.DateField(verbose_name=_('Period End Date'))
    working_days = models.PositiveSmallIntegerField(default=30, verbose_name=_('Total Working Days'))
    paid_days = models.DecimalField(max_digits=5, decimal_places=2, default=30.00, verbose_name=_('Paid Days'))
    unpaid_days = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, verbose_name=_('Unpaid Days'))
    half_days = models.PositiveSmallIntegerField(default=0, verbose_name=_('Half Days'))
    ot_hours = models.DecimalField(max_digits=6, decimal_places=2, default=0.00, verbose_name=_('Overtime Hours'))
    salary_snapshot = models.JSONField(default=dict, blank=True, verbose_name=_('Frozen Salary Snapshot'))
    schedule_snapshot = models.JSONField(default=dict, blank=True, verbose_name=_('Frozen Schedule Snapshot'))
    gross_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name=_('Gross Amount')
    )
    total_deductions = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0.00,
        verbose_name=_('Total Deductions')
    )
    net_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name=_('Net Amount Payable')
    )
    currency = models.CharField(
        max_length=10,
        default='INR',
        verbose_name=_('Currency')
    )
    status = models.CharField(
        max_length=30,
        choices=PayrollStatus.choices,
        default=PayrollStatus.DRAFT,
        verbose_name=_('Payroll Status')
    )
    generated_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Generated At'))

    class Meta:
        verbose_name = _('Payroll')
        verbose_name_plural = _('Payrolls')
        ordering = ['-period_start', 'employee']
        constraints = [
            models.UniqueConstraint(
                fields=['employee', 'period_start', 'period_end'],
                name='unique_employee_payroll_period'
            ),
            models.CheckConstraint(
                condition=models.Q(period_end__gte=models.F('period_start')),
                name='check_payroll_period_valid'
            ),
        ]
        indexes = [
            models.Index(fields=['business', '-period_start', '-period_end'], name='idx_pay_biz_period'),
            models.Index(fields=['employee', '-period_start'], name='idx_pay_emp_period'),
            models.Index(fields=['business', 'status'], name='idx_pay_biz_status'),
        ]

    def __str__(self):
        return f"{self.employee} [{self.period_start} to {self.period_end}]: {self.currency} {self.net_amount}"


class Payslip(TimeStampedUUIDModel):
    """
    Generated payslip document metadata linking to Supabase Storage.
    """
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.PROTECT,
        related_name='payslips',
        verbose_name=_('Business')
    )
    payroll = models.OneToOneField(
        Payroll,
        on_delete=models.PROTECT,
        related_name='payslip',
        verbose_name=_('Payroll Record')
    )
    storage_path = models.CharField(
        max_length=500,
        blank=True,
        verbose_name=_('Supabase Storage Path')
    )
    file_name = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_('File Name')
    )
    generated_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_('Document Generation Timestamp')
    )

    class Meta:
        verbose_name = _('Payslip')
        verbose_name_plural = _('Payslips')
        ordering = ['-created_at']

    def __str__(self):
        return f"Payslip for {self.payroll}"


class EmployeeCompensationItem(TimeStampedUUIDModel):
    """
    Individual custom compensation components (Earnings, Bonuses, Allowances, Deductions, Overtime)
    with fixed or percentage calculation types, effective dates, and audit history.
    """
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.CASCADE,
        related_name='compensation_items',
        verbose_name=_('Business')
    )
    employee = models.ForeignKey(
        'organization.Employee',
        on_delete=models.CASCADE,
        related_name='compensation_items',
        verbose_name=_('Employee')
    )
    name = models.CharField(max_length=150, verbose_name=_('Component Name'))
    component_type = models.CharField(
        max_length=20,
        choices=CompensationComponentType.choices,
        default=CompensationComponentType.EARNING,
        verbose_name=_('Component Type')
    )
    calculation_type = models.CharField(
        max_length=20,
        choices=CompensationCalculationType.choices,
        default=CompensationCalculationType.FIXED_AMOUNT,
        verbose_name=_('Calculation Type')
    )
    frequency = models.CharField(
        max_length=20,
        choices=CompensationFrequency.choices,
        default=CompensationFrequency.RECURRING,
        verbose_name=_('Payment Frequency'),
        help_text=_('RECURRING components recur monthly. ONE_TIME components apply only to their specific effective period.')
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name=_('Amount or Percentage Value')
    )
    effective_from = models.DateField(verbose_name=_('Effective From Date'))
    effective_to = models.DateField(null=True, blank=True, verbose_name=_('Effective To Date'))
    reason = models.TextField(blank=True, verbose_name=_('Reason'))
    notes = models.TextField(blank=True, verbose_name=_('Notes'))
    is_active = models.BooleanField(default=True, verbose_name=_('Is Active'))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_compensation_items',
        verbose_name=_('Created By')
    )

    class Meta:
        verbose_name = _('Employee Compensation Item')
        verbose_name_plural = _('Employee Compensation Items')
        ordering = ['-effective_from', 'name']
        indexes = [
            models.Index(fields=['employee', 'is_active', '-effective_from'], name='idx_comp_emp_act_eff'),
            models.Index(fields=['business', 'component_type'], name='idx_comp_biz_type'),
        ]

    def save(self, *args, **kwargs):
        if not self.business_id and self.employee_id:
            self.business = self.employee.business
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.employee}: {self.name} ({self.component_type}) - {self.amount}"


class PayrollLineItemType(models.TextChoices):
    BASIC = 'BASIC', _('Base Salary')
    EARNING = 'EARNING', _('Earning')
    ALLOWANCE = 'ALLOWANCE', _('Allowance')
    BONUS = 'BONUS', _('Bonus')
    DEDUCTION = 'DEDUCTION', _('Deduction')
    OVERTIME = 'OVERTIME', _('Overtime')
    UNPAID_LEAVE = 'UNPAID_LEAVE', _('Unpaid Leave Deduction')
    PENALTY = 'PENALTY', _('Penalty')
    OTHER = 'OTHER', _('Other')


class PayrollLineItem(TimeStampedUUIDModel):
    """
    Itemized, immutable calculation line for an employee's finalized payroll snapshot.
    Guarantees historical payroll records can never be recalculated or altered by subsequent salary changes.
    """
    payroll = models.ForeignKey(
        Payroll,
        on_delete=models.CASCADE,
        related_name='line_items',
        verbose_name=_('Payroll Record')
    )
    name = models.CharField(max_length=150, verbose_name=_('Component Name'))
    line_type = models.CharField(
        max_length=30,
        choices=PayrollLineItemType.choices,
        default=PayrollLineItemType.EARNING,
        verbose_name=_('Line Type')
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name=_('Amount')
    )
    rate = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0.00,
        verbose_name=_('Unit / Hourly Rate')
    )
    units = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=1.00,
        verbose_name=_('Units / Hours / Days')
    )
    is_deduction = models.BooleanField(
        default=False,
        verbose_name=_('Is Deduction')
    )
    source_compensation_item = models.ForeignKey(
        EmployeeCompensationItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='payroll_lines',
        verbose_name=_('Source Compensation Item')
    )

    class Meta:
        verbose_name = _('Payroll Line Item')
        verbose_name_plural = _('Payroll Line Items')
        ordering = ['is_deduction', 'name']
        indexes = [
            models.Index(fields=['payroll', 'is_deduction'], name='idx_payline_payroll_ded'),
        ]

    def __str__(self):
        sign = '-' if self.is_deduction else '+'
        return f"{self.payroll.employee}: {self.name} ({sign}{self.amount})"


class PayrollScheduleConfig(TimeStampedUUIDModel):
    """
    Unified configuration for payroll cycles, compensation type defaults,
    period boundaries, automatic draft generation, approvals, and disbursement timing.
    Supports Enterprise Default -> Centre Override -> Employee Override hierarchy.
    """
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.CASCADE,
        related_name='payroll_schedule_configs',
        verbose_name=_('Business')
    )
    centre = models.ForeignKey(
        'organization.Branch',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='payroll_schedule_configs',
        verbose_name=_('Centre / Branch')
    )
    employee = models.ForeignKey(
        'organization.Employee',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='payroll_schedule_configs',
        verbose_name=_('Employee')
    )
    scope = models.CharField(
        max_length=20,
        choices=ScheduleConfigScope.choices,
        default=ScheduleConfigScope.ENTERPRISE,
        verbose_name=_('Configuration Scope')
    )
    has_override = models.BooleanField(
        default=True,
        verbose_name=_('Has Active Custom Override'),
        help_text=_('When False, explicitly inherits parent settings (centre inherits enterprise; employee inherits centre).')
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name=_('Is Active')
    )
    effective_from = models.DateField(
        default=timezone.now,
        verbose_name=_('Effective From Date')
    )
    effective_to = models.DateField(
        null=True,
        blank=True,
        verbose_name=_('Effective To Date')
    )

    # 1. Compensation Type
    compensation_type = models.CharField(
        max_length=30,
        choices=CompensationType.choices,
        default=CompensationType.MONTHLY_SALARY,
        verbose_name=_('Compensation Type')
    )

    # 2. Pay Frequency & Cycle
    pay_frequency = models.CharField(
        max_length=30,
        choices=PayFrequency.choices,
        default=PayFrequency.MONTHLY_CALENDAR,
        verbose_name=_('Pay Frequency')
    )
    week_start_day = models.PositiveSmallIntegerField(
        default=0,
        verbose_name=_('Week Start Day'),
        help_text=_('0=Monday, 1=Tuesday, ..., 6=Sunday')
    )
    custom_cycle_start_day = models.PositiveSmallIntegerField(
        default=1,
        verbose_name=_('Custom Cycle Start Day'),
        help_text=_('Day of month (1-31) when recurring cycle starts, e.g. 5th of month to 4th of following month.')
    )
    anchor_date = models.DateField(
        null=True,
        blank=True,
        verbose_name=_('Anchor Date for Fortnightly / Custom Cycles')
    )
    month_end_rule = models.CharField(
        max_length=30,
        choices=MonthEndRule.choices,
        default=MonthEndRule.CLAMP_TO_LAST_DAY,
        verbose_name=_('Month-End Rule')
    )

    # 3. Payroll Draft Generation Schedule
    generation_mode = models.CharField(
        max_length=50,
        choices=PayrollGenerationMode.choices,
        default=PayrollGenerationMode.MANUAL,
        verbose_name=_('Generation Mode')
    )
    generation_delay_days = models.PositiveSmallIntegerField(
        default=1,
        verbose_name=_('Generation Processing Delay (Days after period end)')
    )
    generation_day_of_month = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name=_('Configured Generation Day of Month')
    )

    # 4. Approval Requirements
    approval_required = models.BooleanField(
        default=True,
        verbose_name=_('Approval Required Before Finalization')
    )
    approver_role = models.CharField(
        max_length=30,
        default='BUSINESS_ADMIN',
        verbose_name=_('Approver Role')
    )
    review_deadline_days = models.PositiveSmallIntegerField(
        default=3,
        verbose_name=_('Review Deadline (Days)')
    )

    # 5. Expected Payment Schedule
    payment_rule = models.CharField(
        max_length=30,
        choices=PaymentScheduleRule.choices,
        default=PaymentScheduleRule.DAY_OF_FOLLOWING_MONTH,
        verbose_name=_('Payment Rule')
    )
    payment_offset_days = models.PositiveSmallIntegerField(
        default=5,
        verbose_name=_('Payment Offset Days (after period end)')
    )
    payment_day_of_month = models.PositiveSmallIntegerField(
        default=7,
        verbose_name=_('Payment Day of Following Month')
    )
    payment_weekday = models.PositiveSmallIntegerField(
        default=4,
        verbose_name=_('Specified Payment Weekday (0=Mon, 4=Fri)')
    )

    # 6. Audit & Metadata
    change_reason = models.TextField(
        blank=True,
        verbose_name=_('Change Reason')
    )
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='schedule_configs_changed',
        verbose_name=_('Changed By')
    )

    class Meta:
        verbose_name = _('Payroll Schedule Configuration')
        verbose_name_plural = _('Payroll Schedule Configurations')
        ordering = ['-effective_from', '-created_at']
        indexes = [
            models.Index(fields=['business', 'scope', 'is_active'], name='idx_psched_biz_scope'),
            models.Index(fields=['centre', 'is_active'], name='idx_psched_centre_act'),
            models.Index(fields=['employee', 'is_active'], name='idx_psched_emp_act'),
        ]

    def __str__(self):
        target = f"Employee {self.employee}" if self.employee else (f"Centre {self.centre}" if self.centre else f"Enterprise {self.business.name}")
        return f"{self.scope} [{target}]: {self.get_pay_frequency_display()} ({self.get_compensation_type_display()})"


class PayrollScheduleHistory(TimeStampedUUIDModel):
    """
    Append-only snapshot history preserving every prior configuration state.
    Guarantees historical and finalized payroll records can always verify the exact
    schedule rules in effect on any past date.
    """
    config = models.ForeignKey(
        PayrollScheduleConfig,
        on_delete=models.CASCADE,
        related_name='history_records',
        verbose_name=_('Schedule Configuration')
    )
    business = models.ForeignKey(
        'organization.Business',
        on_delete=models.CASCADE,
        related_name='payroll_schedule_history',
        verbose_name=_('Business')
    )
    centre = models.ForeignKey(
        'organization.Branch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_('Centre / Branch')
    )
    employee = models.ForeignKey(
        'organization.Employee',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_('Employee')
    )
    scope = models.CharField(max_length=20, verbose_name=_('Scope'))
    snapshot = models.JSONField(default=dict, verbose_name=_('Configuration Snapshot'))
    effective_from = models.DateField(verbose_name=_('Effective From Date'))
    effective_to = models.DateField(null=True, blank=True, verbose_name=_('Effective To Date'))
    change_reason = models.TextField(blank=True, verbose_name=_('Change Reason'))
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_('Changed By')
    )

    class Meta:
        verbose_name = _('Payroll Schedule History')
        verbose_name_plural = _('Payroll Schedule History Records')
        ordering = ['-created_at']

    def __str__(self):
        return f"History for {self.config.id} [{self.scope}] at {self.created_at}"

