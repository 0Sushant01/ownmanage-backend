from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedUUIDModel


class BusinessRole(models.TextChoices):
    """
    Role assigned to a user within the scope of a specific business.
    SUPERADMIN is reserved for platform-wide operators.
    """
    SUPERADMIN = 'SUPERADMIN', _('Super Admin')
    BUSINESS_ADMIN = 'BUSINESS_ADMIN', _('Business Admin')
    MANAGER = 'MANAGER', _('Manager')
    STAFF = 'STAFF', _('Staff')
    BROKER = 'BROKER', _('Broker')


class EmploymentStatus(models.TextChoices):
    """
    Official employment status of an employee in a business.
    """
    ACTIVE = 'ACTIVE', _('Active')
    PROBATION = 'PROBATION', _('Probation')
    SUSPENDED = 'SUSPENDED', _('Suspended')
    TERMINATED = 'TERMINATED', _('Terminated')
    RESIGNED = 'RESIGNED', _('Resigned')


class Business(TimeStampedUUIDModel):
    """
    Root multi-tenant entity representing a business / company.
    Every tenant record directly or indirectly links here.
    """
    name = models.CharField(max_length=255, verbose_name=_('Business Name'))
    legal_name = models.CharField(max_length=255, blank=True, verbose_name=_('Legal Name'))
    email = models.EmailField(blank=True, verbose_name=_('Official Email'))
    phone = models.CharField(max_length=30, blank=True, verbose_name=_('Official Phone'))

    address_line_1 = models.CharField(max_length=255, blank=True, verbose_name=_('Address Line 1'))
    address_line_2 = models.CharField(max_length=255, blank=True, verbose_name=_('Address Line 2'))
    city = models.CharField(max_length=100, blank=True, verbose_name=_('City'))
    state = models.CharField(max_length=100, blank=True, verbose_name=_('State / Province'))
    postal_code = models.CharField(max_length=20, blank=True, verbose_name=_('Postal Code'))
    country = models.CharField(max_length=100, default='India', verbose_name=_('Country'))

    # Configurable internationalization & localization settings
    timezone = models.CharField(
        max_length=50,
        default='Asia/Kolkata',
        verbose_name=_('Timezone'),
        help_text=_('IANA timezone identifier for the business (e.g. Asia/Kolkata, UTC, America/New_York).')
    )
    currency = models.CharField(
        max_length=10,
        default='INR',
        verbose_name=_('Currency Code'),
        help_text=_('Standard ISO 4217 currency code (e.g. INR, USD, EUR).')
    )

    # Concurrency-safe Employee ID generation configuration
    employee_id_enabled = models.BooleanField(
        default=True,
        verbose_name=_('Enable Auto Employee IDs'),
        help_text=_('Enable automated generation of sequential employee identifiers.')
    )
    employee_id_prefix = models.CharField(
        max_length=20,
        default='EMP',
        verbose_name=_('Employee ID Prefix'),
        help_text=_('Prefix string attached before the employee number, e.g. EMP, OM.')
    )
    employee_id_next_number = models.PositiveIntegerField(
        default=1,
        verbose_name=_('Next Employee Number'),
        help_text=_('Sequential counter locked during creation to ensure duplicate-free IDs.')
    )

    is_active = models.BooleanField(default=True, verbose_name=_('Is Active'))

    class Meta:
        verbose_name = _('Business')
        verbose_name_plural = _('Businesses')
        ordering = ['name']
        indexes = [
            models.Index(fields=['is_active'], name='idx_biz_is_active'),
        ]

    def __str__(self):
        return self.name


class BusinessMembership(TimeStampedUUIDModel):
    """
    Associates a User with a Business and defines their role and membership lifecycle.
    Crucial multi-tenancy anchor ensuring users only access their assigned businesses.
    """
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name='memberships',
        verbose_name=_('Business')
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='business_memberships',
        verbose_name=_('User')
    )
    role = models.CharField(
        max_length=30,
        choices=BusinessRole.choices,
        default=BusinessRole.STAFF,
        verbose_name=_('Role')
    )
    is_active = models.BooleanField(default=True, verbose_name=_('Is Active'))
    joined_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Joined At'))
    left_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Left At'))

    class Meta:
        verbose_name = _('Business Membership')
        verbose_name_plural = _('Business Memberships')
        constraints = [
            models.UniqueConstraint(
                fields=['business', 'user'],
                name='unique_business_user_membership'
            )
        ]
        indexes = [
            models.Index(fields=['business', 'role', 'is_active'], name='idx_biz_member_role'),
            models.Index(fields=['user', 'is_active'], name='idx_user_member_active'),
        ]

    def __str__(self):
        return f"{self.user} in {self.business} as {self.get_role_display()}"


class Branch(TimeStampedUUIDModel):
    """
    Physical or logical work locations belonging to a business.
    Supports single-branch businesses as well as multi-branch enterprises.
    """
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name='branches',
        verbose_name=_('Business')
    )
    name = models.CharField(max_length=255, verbose_name=_('Branch Name'))
    code = models.CharField(max_length=50, verbose_name=_('Branch Code'))
    address = models.TextField(blank=True, verbose_name=_('Address'))
    city = models.CharField(max_length=100, blank=True, verbose_name=_('City'))
    state = models.CharField(max_length=100, blank=True, verbose_name=_('State'))
    postal_code = models.CharField(max_length=20, blank=True, verbose_name=_('Postal Code'))
    country = models.CharField(max_length=100, default='India', verbose_name=_('Country'))
    phone = models.CharField(max_length=30, blank=True, verbose_name=_('Contact Phone'))
    email = models.EmailField(blank=True, verbose_name=_('Contact Email'))
    status = models.CharField(
        max_length=20,
        choices=[('ACTIVE', 'Active'), ('INACTIVE', 'Inactive'), ('SUSPENDED', 'Suspended')],
        default='ACTIVE',
        verbose_name=_('Centre Status')
    )
    opening_date = models.DateField(null=True, blank=True, verbose_name=_('Opening Date'))
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, verbose_name=_('Latitude'))
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True, verbose_name=_('Longitude'))
    geofence_radius = models.PositiveIntegerField(default=100, verbose_name=_('Allowed Geofence Radius (meters)'))
    currency = models.CharField(max_length=10, default='INR', verbose_name=_('Currency'))
    timezone = models.CharField(
        max_length=50,
        blank=True,
        verbose_name=_('Branch Timezone Override'),
        help_text=_('Leave empty to inherit the parent business timezone.')
    )
    is_active = models.BooleanField(default=True, verbose_name=_('Is Active'))

    class Meta:
        verbose_name = _('Branch')
        verbose_name_plural = _('Branches')
        ordering = ['business', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['business', 'code'],
                name='unique_business_branch_code'
            )
        ]
        indexes = [
            models.Index(fields=['business', 'is_active'], name='idx_branch_biz_active'),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"

    @property
    def allocated_capacity(self) -> int:
        if hasattr(self, 'capacity_allocation'):
            return self.capacity_allocation.allocated_capacity
        return 0

    @property
    def active_employees_count(self) -> int:
        return self.employees.filter(employment_status='ACTIVE').count()


# Centre is the canonical operational name for a work location / branch
Centre = Branch


class Department(TimeStampedUUIDModel):
    """
    Functional organizational units within a business.
    """
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name='departments',
        verbose_name=_('Business')
    )
    name = models.CharField(max_length=255, verbose_name=_('Department Name'))
    code = models.CharField(max_length=50, verbose_name=_('Department Code'))
    description = models.TextField(blank=True, verbose_name=_('Description'))
    is_active = models.BooleanField(default=True, verbose_name=_('Is Active'))

    class Meta:
        verbose_name = _('Department')
        verbose_name_plural = _('Departments')
        ordering = ['business', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['business', 'name'],
                name='unique_business_dept_name'
            ),
            models.UniqueConstraint(
                fields=['business', 'code'],
                name='unique_business_dept_code'
            ),
        ]
        indexes = [
            models.Index(fields=['business', 'is_active'], name='idx_dept_biz_active'),
        ]

    def __str__(self):
        return f"{self.name} ({self.business})"


class Employee(TimeStampedUUIDModel):
    """
    Domain employee profile within a business.
    Separated from authentication identity (User can be created or linked later).
    Protected against accidental cascade deletion to preserve payroll and attendance history.
    """
    business = models.ForeignKey(
        Business,
        on_delete=models.PROTECT,
        related_name='employees',
        verbose_name=_('Business')
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='employee_profiles',
        verbose_name=_('Linked User Account'),
        help_text=_('Authentication user linked to this employee. Nullable if employee has not been invited yet.')
    )
    employee_id = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        verbose_name=_('Employee ID / Code'),
        help_text=_('Business-scoped employee code (e.g. EMP001). Optional if business disables employee IDs.')
    )
    first_name = models.CharField(max_length=150, verbose_name=_('First Name'))
    last_name = models.CharField(max_length=150, blank=True, verbose_name=_('Last Name'))
    email = models.EmailField(blank=True, verbose_name=_('Work Email'))
    phone = models.CharField(max_length=30, blank=True, verbose_name=_('Phone Number'))

    branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='employees',
        verbose_name=_('Branch')
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='employees',
        verbose_name=_('Department')
    )
    designation = models.CharField(max_length=100, verbose_name=_('Designation / Job Title'))
    manager = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='direct_reports',
        verbose_name=_('Reporting Manager')
    )

    joining_date = models.DateField(verbose_name=_('Joining Date'))
    employment_status = models.CharField(
        max_length=30,
        choices=EmploymentStatus.choices,
        default=EmploymentStatus.ACTIVE,
        verbose_name=_('Employment Status')
    )
    status_reason = models.TextField(blank=True, verbose_name=_('Status Change Reason'))
    status_effective_date = models.DateField(null=True, blank=True, verbose_name=_('Status Effective Date'))
    date_of_exit = models.DateField(null=True, blank=True, verbose_name=_('Date of Exit'))

    class Meta:
        verbose_name = _('Employee')
        verbose_name_plural = _('Employees')
        ordering = ['first_name', 'last_name']
        constraints = [
            models.UniqueConstraint(
                fields=['business', 'employee_id'],
                condition=models.Q(employee_id__isnull=False),
                name='unique_business_employee_id'
            ),
        ]
        indexes = [
            models.Index(fields=['business', 'employment_status'], name='idx_emp_biz_status'),
            models.Index(fields=['business', 'manager'], name='idx_emp_biz_manager'),
            models.Index(fields=['business', 'department'], name='idx_emp_biz_dept'),
            models.Index(fields=['business', 'branch'], name='idx_emp_biz_branch'),
            models.Index(fields=['user'], name='idx_emp_user'),
        ]

    def __str__(self):
        code = f" [{self.employee_id}]" if self.employee_id else ""
        return f"{self.first_name} {self.last_name}{code}".strip()

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class EmployeeAssignment(TimeStampedUUIDModel):
    """
    Historical log of employee organizational assignments (manager, branch, department, designation).
    Ensures that historical payroll, audit, and attendance records do not change when an
    employee transfers departments or changes reporting managers.
    """
    business = models.ForeignKey(
        Business,
        on_delete=models.PROTECT,
        related_name='employee_assignments',
        verbose_name=_('Business')
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='assignments',
        verbose_name=_('Employee')
    )
    manager = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
        verbose_name=_('Manager at this period')
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
        verbose_name=_('Branch at this period')
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
        verbose_name=_('Department at this period')
    )
    designation = models.CharField(max_length=100, verbose_name=_('Designation'))
    effective_from = models.DateField(verbose_name=_('Effective From'))
    effective_to = models.DateField(null=True, blank=True, verbose_name=_('Effective To'))
    notes = models.TextField(blank=True, verbose_name=_('Change Notes'))

    class Meta:
        verbose_name = _('Employee Assignment')
        verbose_name_plural = _('Employee Assignments')
        ordering = ['-effective_from']
        indexes = [
            models.Index(fields=['employee', '-effective_from'], name='idx_emp_assign_eff_from'),
            models.Index(fields=['business', '-effective_from'], name='idx_biz_assign_eff_from'),
        ]

    def __str__(self):
        return f"{self.employee} ({self.effective_from} to {self.effective_to or 'Present'})"


class Designation(TimeStampedUUIDModel):
    """
    Formal organizational designation / job title configured by Enterprise.
    Enforces uniqueness per enterprise.
    """
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name='designation_records',
        verbose_name=_('Business')
    )
    name = models.CharField(max_length=150, verbose_name=_('Designation Name'))
    code = models.CharField(max_length=50, blank=True, verbose_name=_('Designation Code'))
    description = models.TextField(blank=True, verbose_name=_('Description'))
    is_active = models.BooleanField(default=True, verbose_name=_('Is Active'))

    class Meta:
        verbose_name = _('Designation')
        verbose_name_plural = _('Designations')
        ordering = ['business', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['business', 'name'],
                name='unique_business_designation_name'
            )
        ]
        indexes = [
            models.Index(fields=['business', 'is_active'], name='idx_desig_biz_active'),
        ]

    def __str__(self):
        return f"{self.name} ({self.business.name})"


class PermissionScope(models.TextChoices):
    GLOBAL = 'GLOBAL', _('Global / SaaS-Wide')
    ENTERPRISE = 'ENTERPRISE', _('Enterprise / Business')
    CENTER = 'CENTER', _('Center / Branch')
    SELF = 'SELF', _('Self / Own Record')


class Permission(TimeStampedUUIDModel):
    """
    Canonical system permissions grouped by functional module.
    """
    key = models.CharField(max_length=100, unique=True, db_index=True, verbose_name=_('Permission Key'))
    name = models.CharField(max_length=150, verbose_name=_('Permission Name'))
    module = models.CharField(max_length=50, db_index=True, verbose_name=_('Module Group'))
    description = models.TextField(blank=True, verbose_name=_('Description'))
    default_scope = models.CharField(
        max_length=30,
        choices=PermissionScope.choices,
        default=PermissionScope.CENTER,
        verbose_name=_('Default Scope')
    )

    class Meta:
        verbose_name = _('Permission')
        verbose_name_plural = _('Permissions')
        ordering = ['module', 'key']

    def __str__(self):
        return f"{self.module}.{self.key} ({self.name})"


class ManagerAccessControl(TimeStampedUUIDModel):
    """
    Enterprise-controlled granular permissions for Center Managers.
    Enterprise Admin configures which operations a specific manager can perform in their assigned center.
    """
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name='manager_access_controls',
        verbose_name=_('Business')
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='manager_permissions',
        verbose_name=_('Manager User')
    )
    permission = models.ForeignKey(
        Permission,
        on_delete=models.CASCADE,
        related_name='manager_grants',
        verbose_name=_('Permission')
    )
    is_granted = models.BooleanField(default=True, verbose_name=_('Is Granted'))
    scope = models.CharField(
        max_length=30,
        choices=PermissionScope.choices,
        default=PermissionScope.CENTER,
        verbose_name=_('Scope')
    )
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='granted_permissions',
        verbose_name=_('Granted By')
    )

    class Meta:
        verbose_name = _('Manager Access Control')
        verbose_name_plural = _('Manager Access Controls')
        constraints = [
            models.UniqueConstraint(
                fields=['business', 'user', 'permission'],
                name='unique_business_user_permission'
            )
        ]
        indexes = [
            models.Index(fields=['business', 'user', 'is_granted'], name='idx_mgr_access_lookup'),
        ]

    def __str__(self):
        status = "Granted" if self.is_granted else "Denied"
        return f"{self.user} - {self.permission.key}: {status} ({self.scope})"


class DocumentCategory(models.TextChoices):
    IDENTITY_PROOF = 'IDENTITY_PROOF', _('Identity Proof')
    ADDRESS_PROOF = 'ADDRESS_PROOF', _('Address Proof')
    EDUCATION = 'EDUCATION', _('Education Certificate')
    CONTRACT = 'CONTRACT', _('Employment Contract')
    OFFER_LETTER = 'OFFER_LETTER', _('Offer Letter')
    EXPERIENCE = 'EXPERIENCE', _('Experience Certificate')
    BANK = 'BANK', _('Bank Document')
    TAX = 'TAX', _('Tax Document')
    OTHER = 'OTHER', _('Other')


class VerificationStatus(models.TextChoices):
    PENDING = 'PENDING', _('Pending Verification')
    VERIFIED = 'VERIFIED', _('Verified')
    REJECTED = 'REJECTED', _('Rejected')


class EmployeeDocument(TimeStampedUUIDModel):
    """
    Employee document repository with verification workflow and soft deletion.
    """
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name='employee_documents',
        verbose_name=_('Business')
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='documents',
        verbose_name=_('Employee')
    )
    title = models.CharField(max_length=255, verbose_name=_('Document Title'))
    category = models.CharField(
        max_length=50,
        choices=DocumentCategory.choices,
        default=DocumentCategory.OTHER,
        verbose_name=_('Category')
    )
    file_url = models.CharField(max_length=1000, verbose_name=_('File URL / Storage Path'))
    file_name = models.CharField(max_length=255, verbose_name=_('File Name'))
    file_size_bytes = models.PositiveIntegerField(default=0, verbose_name=_('File Size (Bytes)'))
    issue_date = models.DateField(null=True, blank=True, verbose_name=_('Issue Date'))
    expiry_date = models.DateField(null=True, blank=True, verbose_name=_('Expiry Date'))
    verification_status = models.CharField(
        max_length=30,
        choices=VerificationStatus.choices,
        default=VerificationStatus.PENDING,
        verbose_name=_('Verification Status')
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='verified_documents',
        verbose_name=_('Verified By')
    )
    verified_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Verified At'))
    remarks = models.TextField(blank=True, verbose_name=_('Remarks / Verification Notes'))
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='uploaded_documents',
        verbose_name=_('Uploaded By')
    )
    is_deleted = models.BooleanField(default=False, verbose_name=_('Is Soft Deleted'))
    deleted_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Deleted At'))
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='deleted_documents',
        verbose_name=_('Deleted By')
    )

    class Meta:
        verbose_name = _('Employee Document')
        verbose_name_plural = _('Employee Documents')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['employee', 'is_deleted'], name='idx_emp_doc_active'),
            models.Index(fields=['business', 'category', 'is_deleted'], name='idx_biz_doc_cat'),
        ]

    def __str__(self):
        return f"{self.title} ({self.get_category_display()}) - {self.employee}"


class Holiday(TimeStampedUUIDModel):
    """
    Holiday calendar configured by Enterprise.
    Supports platform/enterprise wide holidays or center-specific applicability.
    """
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name='holidays',
        verbose_name=_('Business')
    )
    name = models.CharField(max_length=150, verbose_name=_('Holiday Name'))
    holiday_date = models.DateField(verbose_name=_('Holiday Date'))
    description = models.TextField(blank=True, verbose_name=_('Description'))
    is_optional = models.BooleanField(default=False, verbose_name=_('Is Optional / Restricted'))
    applies_to_all_centres = models.BooleanField(
        default=True,
        verbose_name=_('Applies to All Centres'),
        help_text=_('If True, holiday applies enterprise-wide. If False, only attached centres observe it.')
    )
    centres = models.ManyToManyField(
        Branch,
        blank=True,
        related_name='holidays',
        verbose_name=_('Specific Centres')
    )

    class Meta:
        verbose_name = _('Holiday')
        verbose_name_plural = _('Holidays')
        ordering = ['holiday_date', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['business', 'holiday_date', 'name'],
                name='unique_business_holiday_date_name'
            )
        ]
        indexes = [
            models.Index(fields=['business', 'holiday_date'], name='idx_holiday_biz_date'),
        ]

    def __str__(self):
        scope = "All Centres" if self.applies_to_all_centres else "Selected Centres"
        return f"{self.name} on {self.holiday_date} ({scope})"


class EmployeeActivityLog(TimeStampedUUIDModel):
    """
    Chronological activity timeline tracking significant employee lifecycle changes.
    Section 23: Tracks salary changes, centre changes, manager reassignments, and status modifications.
    """
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name='employee_activities',
        verbose_name=_('Business')
    )
    employee = models.ForeignKey(
        'organization.Employee',
        on_delete=models.CASCADE,
        related_name='activity_logs',
        verbose_name=_('Employee')
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='authored_employee_activities',
        verbose_name=_('Actor')
    )
    action_type = models.CharField(max_length=50, verbose_name=_('Action Type'))
    description = models.CharField(max_length=255, verbose_name=_('Description'))
    old_value = models.TextField(blank=True, verbose_name=_('Old Value'))
    new_value = models.TextField(blank=True, verbose_name=_('New Value'))
    metadata = models.JSONField(default=dict, blank=True, verbose_name=_('Metadata'))

    class Meta:
        verbose_name = _('Employee Activity Log')
        verbose_name_plural = _('Employee Activity Logs')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['employee', '-created_at'], name='idx_emp_act_created'),
            models.Index(fields=['business', '-created_at'], name='idx_biz_act_created'),
        ]

    def __str__(self):
        return f"{self.employee.full_name}: {self.action_type} at {self.created_at}"

