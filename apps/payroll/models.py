from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedUUIDModel


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


class Payroll(TimeStampedUUIDModel):
    """
    Frozen, immutable snapshot of employee remuneration for an arbitrary time period.
    """
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
