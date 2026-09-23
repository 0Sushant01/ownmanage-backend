from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from apps.accounts.views import (
    LoginView, LogoutView, CurrentUserView, ProfileView,
    ForgotPasswordView, VerifyOTPView, ResetPasswordView, ActivateAccountView
)
from apps.organization.views import (
    BusinessListCreateView, BusinessDetailView, BusinessStatsView, BusinessCreateAdminView,
    ManagerListView, ManagerDetailView,
    EmployeeListView, EmployeeDetailView, EmployeeDeactivateView, EmployeeMetadataView,
    CentreListCreateView, CentreDetailView, DepartmentListCreateView
)
from apps.attendance.views import (
    AttendanceTodayView, AttendanceCheckInView, AttendanceCheckOutView,
    AttendanceHistoryView, AttendanceCalendarView,
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
    CommissionListView
)
from apps.subscriptions.analytics_views import SuperAdminAnalyticsView
from apps.core.views import (
    NotificationListView, NotificationMarkReadView
)

urlpatterns = [
    # Authentication & Session
    path('auth/login/', LoginView.as_view(), name='api-login'),
    path('auth/refresh/', TokenRefreshView.as_view(), name='api-token-refresh'),
    path('auth/logout/', LogoutView.as_view(), name='api-logout'),
    path('auth/me/', CurrentUserView.as_view(), name='api-current-user'),

    # OTP Account Activation & Password Reset
    path('auth/forgot-password/', ForgotPasswordView.as_view(), name='api-forgot-password'),
    path('auth/verify-otp/', VerifyOTPView.as_view(), name='api-verify-otp'),
    path('auth/reset-password/', ResetPasswordView.as_view(), name='api-reset-password'),
    path('auth/activate/', ActivateAccountView.as_view(), name='api-activate-account'),

    # Profile
    path('profile/', ProfileView.as_view(), name='api-profile'),

    # Businesses / Enterprises (SuperAdmin & Business Admin)
    path('businesses/', BusinessListCreateView.as_view(), name='api-businesses'),
    path('businesses/stats/', BusinessStatsView.as_view(), name='api-global-stats'),
    path('businesses/<uuid:pk>/', BusinessDetailView.as_view(), name='api-business-detail'),
    path('businesses/<uuid:pk>/stats/', BusinessStatsView.as_view(), name='api-business-stats'),
    path('businesses/<uuid:pk>/create-admin/', BusinessCreateAdminView.as_view(), name='api-business-create-admin'),

    # Plans & Subscriptions (SuperAdmin & Enterprise Admin)
    path('plans/', PlanListCreateView.as_view(), name='api-plans'),
    path('plans/<uuid:pk>/', PlanDetailView.as_view(), name='api-plan-detail'),
    path('subscriptions/', SubscriptionDetailView.as_view(), name='api-subscriptions'),
    path('subscriptions/reallocate-capacity/', CapacityReallocateView.as_view(), name='api-capacity-reallocate'),

    # Brokers & Commissions (Platform SuperAdmin & Broker isolated)
    path('brokers/', BrokerListCreateView.as_view(), name='api-brokers'),
    path('brokers/dashboard/', BrokerDashboardView.as_view(), name='api-broker-dashboard'),
    path('commissions/', CommissionListView.as_view(), name='api-commissions'),

    # SuperAdmin SaaS Analytics
    path('analytics/superadmin/', SuperAdminAnalyticsView.as_view(), name='api-superadmin-analytics'),

    # Managers
    path('managers/', ManagerListView.as_view(), name='api-managers'),
    path('managers/<uuid:pk>/', ManagerDetailView.as_view(), name='api-manager-detail'),

    # Centres / Branches
    path('centres/', CentreListCreateView.as_view(), name='api-centres'),
    path('centres/<uuid:pk>/', CentreDetailView.as_view(), name='api-centre-detail'),

    # Departments
    path('departments/', DepartmentListCreateView.as_view(), name='api-departments'),

    # Employees
    path('employees/', EmployeeListView.as_view(), name='api-employees'),
    path('employees/metadata/', EmployeeMetadataView.as_view(), name='api-employee-metadata'),
    path('employees/<uuid:pk>/', EmployeeDetailView.as_view(), name='api-employee-detail'),
    path('employees/<uuid:pk>/deactivate/', EmployeeDeactivateView.as_view(), name='api-employee-deactivate'),

    # Work Schedules
    path('schedules/', WorkScheduleListView.as_view(), name='api-schedules'),

    # Attendance
    path('attendance/today/', AttendanceTodayView.as_view(), name='api-attendance-today'),
    path('attendance/check-in/', AttendanceCheckInView.as_view(), name='api-attendance-checkin'),
    path('attendance/check-out/', AttendanceCheckOutView.as_view(), name='api-attendance-checkout'),
    path('attendance/history/', AttendanceHistoryView.as_view(), name='api-attendance-history'),
    path('attendance/calendar/', AttendanceCalendarView.as_view(), name='api-attendance-calendar'),

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

    # Notifications
    path('notifications/', NotificationListView.as_view(), name='api-notifications'),
    path('notifications/<uuid:pk>/mark-read/', NotificationMarkReadView.as_view(), name='api-notification-mark-read'),
]
