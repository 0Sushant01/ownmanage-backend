from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from apps.accounts.views import (
    LoginView, LogoutView, CurrentUserView, ProfileView,
    ForgotPasswordView, VerifyOTPView, ResetPasswordView, ActivateAccountView,
    ChangePasswordView, EmailUpdateView
)
from apps.organization.views import (
    BusinessListCreateView, BusinessDetailView, BusinessStatsView, BusinessCreateAdminView,
    ManagerListView, ManagerDetailView,
    EmployeeListView, EmployeeDetailView, EmployeeDeactivateView, EmployeeMetadataView,
    CentreListCreateView, CentreDetailView, DepartmentListCreateView,
    BusinessCentresCapacityView, EmployeeActivityLogView
)
from apps.attendance.views import (
    AttendanceTodayView, AttendanceCheckInView, AttendanceCheckOutView,
    AttendanceHistoryView, AttendanceCalendarView, AttendanceDailyRegisterView,
    WorkScheduleListView, AttendanceCorrectionListCreateView,
    AttendanceCorrectionApproveView, AttendanceCorrectionRejectView
)
from apps.leaves.views import (
    LeaveTypeListView, LeaveRequestListCreateView,
    LeaveRequestApproveView, LeaveRequestRejectView
)
from apps.payroll.views import (
    PayrollListView, PayrollDetailView
)
from apps.subscriptions.views import (
    PlanListCreateView, PlanDetailView, SubscriptionDetailView,
    CapacityReallocateView, BrokerListCreateView, BrokerDashboardView,
    CommissionListView, PlanSubscribersView, BrokerDetailView,
    PayCommissionView, BusinessSubscriptionHistoryView
)
from apps.subscriptions.analytics_views import SuperAdminAnalyticsView
from apps.core.views import (
    NotificationListView, NotificationMarkReadView, AuditLogListView
)
from apps.organization.access_views import (
    PermissionListView, ManagerAccessControlView,
    DesignationListCreateView, DesignationDetailView,
    EmployeeDocumentListCreateView, EmployeeDocumentDetailView, EmployeeDocumentVerifyView,
    HolidayListCreateView, HolidayDetailView
)
from apps.attendance.policy_views import (
    EnterpriseAttendancePolicyView, CentreAttendancePolicyView, CentreAttendancePolicyResetView
)
from apps.payroll.revision_views import (
    EmployeeSalaryRevisionListCreateView, PayrollRunListCreateView,
    PayrollRunApproveView, PayrollRunFinalizeView,
    EmployeeSalaryComparisonView, EmployeeCompensationItemListCreateView,
    EmployeeCompensationItemDetailView
)

urlpatterns = [
    # Authentication & Session
    path('auth/login/', LoginView.as_view(), name='api-login'),
    path('auth/refresh/', TokenRefreshView.as_view(), name='api-token-refresh'),
    path('auth/logout/', LogoutView.as_view(), name='api-logout'),
    path('auth/me/', CurrentUserView.as_view(), name='api-current-user'),
    path('auth/change-password/', ChangePasswordView.as_view(), name='api-change-password'),
    path('auth/update-email/', EmailUpdateView.as_view(), name='api-update-email'),

    # OTP Account Activation & Password Reset
    path('auth/forgot-password/', ForgotPasswordView.as_view(), name='api-forgot-password'),
    path('auth/verify-otp/', VerifyOTPView.as_view(), name='api-verify-otp'),
    path('auth/reset-password/', ResetPasswordView.as_view(), name='api-reset-password'),
    path('auth/activate/', ActivateAccountView.as_view(), name='api-activate-account'),

    # Profile & Audit Trail
    path('profile/', ProfileView.as_view(), name='api-profile'),
    path('audit-logs/', AuditLogListView.as_view(), name='api-audit-logs'),

    # Businesses / Enterprises (SuperAdmin & Business Admin)
    path('businesses/', BusinessListCreateView.as_view(), name='api-businesses'),
    path('businesses/stats/', BusinessStatsView.as_view(), name='api-global-stats'),
    path('businesses/<uuid:pk>/', BusinessDetailView.as_view(), name='api-business-detail'),
    path('businesses/<uuid:pk>/stats/', BusinessStatsView.as_view(), name='api-business-stats'),
    path('businesses/<uuid:pk>/create-admin/', BusinessCreateAdminView.as_view(), name='api-business-create-admin'),
    path('businesses/<uuid:pk>/subscription-history/', BusinessSubscriptionHistoryView.as_view(), name='api-business-sub-history'),
    path('businesses/<uuid:pk>/centres/', BusinessCentresCapacityView.as_view(), name='api-business-centres'),

    # Plans & Subscriptions (SuperAdmin & Enterprise Admin)
    path('plans/', PlanListCreateView.as_view(), name='api-plans'),
    path('plans/<uuid:pk>/', PlanDetailView.as_view(), name='api-plan-detail'),
    path('plans/<uuid:pk>/subscribers/', PlanSubscribersView.as_view(), name='api-plan-subscribers'),
    path('subscriptions/', SubscriptionDetailView.as_view(), name='api-subscriptions'),
    path('subscriptions/reallocate-capacity/', CapacityReallocateView.as_view(), name='api-capacity-reallocate'),

    # Brokers & Commissions (Platform SuperAdmin & Broker isolated)
    path('brokers/', BrokerListCreateView.as_view(), name='api-brokers'),
    path('brokers/<uuid:pk>/', BrokerDetailView.as_view(), name='api-broker-detail'),
    path('brokers/dashboard/', BrokerDashboardView.as_view(), name='api-broker-dashboard'),
    path('commissions/', CommissionListView.as_view(), name='api-commissions'),
    path('commissions/<uuid:pk>/pay/', PayCommissionView.as_view(), name='api-commission-pay'),

    # SuperAdmin SaaS Analytics
    path('analytics/superadmin/', SuperAdminAnalyticsView.as_view(), name='api-superadmin-analytics'),


    # Managers
    path('managers/', ManagerListView.as_view(), name='api-managers'),
    path('managers/<uuid:pk>/', ManagerDetailView.as_view(), name='api-manager-detail'),
    path('managers/<uuid:pk>/access-control/', ManagerAccessControlView.as_view(), name='api-manager-access-control'),

    # Permissions & Granular Access Control
    path('permissions/', PermissionListView.as_view(), name='api-permissions'),

    # Centres / Branches
    path('centres/', CentreListCreateView.as_view(), name='api-centres'),
    path('centres/<uuid:pk>/', CentreDetailView.as_view(), name='api-centre-detail'),
    path('centres/<uuid:pk>/attendance-policy/', CentreAttendancePolicyView.as_view(), name='api-centre-attendance-policy'),
    path('centres/<uuid:pk>/attendance-policy/reset/', CentreAttendancePolicyResetView.as_view(), name='api-centre-attendance-policy-reset'),

    # Departments & Designations
    path('departments/', DepartmentListCreateView.as_view(), name='api-departments'),
    path('designations/', DesignationListCreateView.as_view(), name='api-designations'),
    path('designations/<uuid:pk>/', DesignationDetailView.as_view(), name='api-designation-detail'),

    # Holidays
    path('holidays/', HolidayListCreateView.as_view(), name='api-holidays'),
    path('holidays/<uuid:pk>/', HolidayDetailView.as_view(), name='api-holiday-detail'),

    # Employees
    path('employees/', EmployeeListView.as_view(), name='api-employees'),
    path('employees/metadata/', EmployeeMetadataView.as_view(), name='api-employee-metadata'),
    path('employees/<uuid:pk>/', EmployeeDetailView.as_view(), name='api-employee-detail'),
    path('employees/<uuid:pk>/deactivate/', EmployeeDeactivateView.as_view(), name='api-employee-deactivate'),
    path('employees/<uuid:pk>/documents/', EmployeeDocumentListCreateView.as_view(), name='api-employee-documents'),
    path('employees/<uuid:pk>/activity/', EmployeeActivityLogView.as_view(), name='api-employee-activity'),
    path('employees/<uuid:pk>/salary-revisions/', EmployeeSalaryRevisionListCreateView.as_view(), name='api-employee-salary-revisions'),
    path('employees/<uuid:pk>/salary-comparison/', EmployeeSalaryComparisonView.as_view(), name='api-employee-salary-comparison'),
    path('employees/<uuid:pk>/compensation-items/', EmployeeCompensationItemListCreateView.as_view(), name='api-employee-comp-items'),
    path('employees/<uuid:pk>/compensation-items/<uuid:comp_pk>/', EmployeeCompensationItemDetailView.as_view(), name='api-employee-comp-item-detail'),

    # Documents
    path('documents/<uuid:pk>/', EmployeeDocumentDetailView.as_view(), name='api-document-detail'),
    path('documents/<uuid:pk>/verify/', EmployeeDocumentVerifyView.as_view(), name='api-document-verify'),

    # Work Schedules
    path('schedules/', WorkScheduleListView.as_view(), name='api-schedules'),

    # Attendance
    path('attendance/register/', AttendanceDailyRegisterView.as_view(), name='api-attendance-register'),
    path('attendance/daily-register/', AttendanceDailyRegisterView.as_view(), name='api-attendance-daily-register'),
    path('attendance/today/', AttendanceTodayView.as_view(), name='api-attendance-today'),
    path('attendance/check-in/', AttendanceCheckInView.as_view(), name='api-attendance-checkin'),
    path('attendance/check-out/', AttendanceCheckOutView.as_view(), name='api-attendance-checkout'),
    path('attendance/history/', AttendanceHistoryView.as_view(), name='api-attendance-history'),
    path('attendance/calendar/', AttendanceCalendarView.as_view(), name='api-attendance-calendar'),
    path('attendance/policies/enterprise/', EnterpriseAttendancePolicyView.as_view(), name='api-attendance-policy-enterprise'),

    # Attendance Corrections
    path('attendance/corrections/', AttendanceCorrectionListCreateView.as_view(), name='api-attendance-corrections'),
    path('attendance/corrections/<uuid:pk>/approve/', AttendanceCorrectionApproveView.as_view(), name='api-attendance-correction-approve'),
    path('attendance/corrections/<uuid:pk>/reject/', AttendanceCorrectionRejectView.as_view(), name='api-attendance-correction-reject'),

    # Leaves
    path('leaves/types/', LeaveTypeListView.as_view(), name='api-leave-types'),
    path('leaves/requests/', LeaveRequestListCreateView.as_view(), name='api-leaves'),
    path('leaves/requests/<uuid:pk>/approve/', LeaveRequestApproveView.as_view(), name='api-leave-approve'),
    path('leaves/requests/<uuid:pk>/reject/', LeaveRequestRejectView.as_view(), name='api-leave-reject'),

    # Salary & Payroll
    path('salary/payrolls/', PayrollListView.as_view(), name='api-salary-payrolls'),
    path('salary/payrolls/<uuid:pk>/', PayrollDetailView.as_view(), name='api-salary-detail'),
    path('payroll/runs/', PayrollRunListCreateView.as_view(), name='api-payroll-runs'),
    path('payroll/runs/<uuid:pk>/approve/', PayrollRunApproveView.as_view(), name='api-payroll-run-approve'),
    path('payroll/runs/<uuid:pk>/finalize/', PayrollRunFinalizeView.as_view(), name='api-payroll-run-finalize'),

    # Notifications
    path('notifications/', NotificationListView.as_view(), name='api-notifications'),
    path('notifications/<uuid:pk>/mark-read/', NotificationMarkReadView.as_view(), name='api-notification-mark-read'),
]
