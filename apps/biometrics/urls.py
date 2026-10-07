from django.urls import path
from apps.biometrics.views import (
    BiometricModelManifestView,
    EmployeeBiometricStatusView,
    EmployeeFaceEnrollView,
    EmployeeFaceRevokeView,
    DeviceRegisterView,
    DeviceRevokeView,
    BiometricTemplateProvisionView,
    BiometricChallengeView,
    BiometricAuditListView
)

app_name = 'biometrics'

urlpatterns = [
    path('manifest/', BiometricModelManifestView.as_view(), name='manifest'),
    path('employees/<uuid:employee_id>/status/', EmployeeBiometricStatusView.as_view(), name='employee-status'),
    path('employees/<uuid:employee_id>/enroll/', EmployeeFaceEnrollView.as_view(), name='employee-enroll'),
    path('employees/<uuid:employee_id>/revoke/', EmployeeFaceRevokeView.as_view(), name='employee-revoke'),
    path('devices/register/', DeviceRegisterView.as_view(), name='device-register'),
    path('devices/revoke/', DeviceRevokeView.as_view(), name='device-revoke'),
    path('template/provision/', BiometricTemplateProvisionView.as_view(), name='template-provision'),
    path('challenge/', BiometricChallengeView.as_view(), name='challenge'),
    path('audit/', BiometricAuditListView.as_view(), name='audit-list'),
]
