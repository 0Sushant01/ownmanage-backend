"""
Biometrics Subsystem API Views.
Handles model manifest, face enrollment, revocation, device authorization,
template provisioning, and challenge nonces.
"""
from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404

from apps.core.permissions import get_user_context
from apps.organization.models import Employee, BusinessRole
from apps.organization.services.permission_service import PermissionService
from apps.biometrics.models import (
    EmployeeFaceEnrollment,
    EmployeeDevice,
    BiometricStatus,
    DeviceStatus,
    BiometricAuditEvent
)
from apps.biometrics.serializers import (
    EmployeeFaceEnrollmentSerializer,
    EmployeeDeviceSerializer,
    BiometricAuditEventSerializer,
    DeviceRegistrationInputSerializer,
    RevokeDeviceInputSerializer
)
from apps.biometrics.services.model_registry import ModelRegistry
from apps.biometrics.services.enrollment_service import EnrollmentService
from apps.biometrics.services.template_service import TemplateService
from apps.biometrics.services.device_service import DeviceService
from apps.biometrics.services.verification_service import VerificationService


def _get_client_ip(request) -> str:
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded:
        return x_forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


def _can_enroll_face(request, employee) -> bool:
    """Checks whether requesting user can enroll/re-enroll/revoke face for this employee."""
    if not request.user or not request.user.is_authenticated:
        return False
    if request.user.is_superuser:
        return True

    biz = employee.business
    ctx = get_user_context(request)

    if ctx.get('role') == BusinessRole.BUSINESS_ADMIN:
        return True

    if ctx.get('role') == BusinessRole.MANAGER:
        return PermissionService.has_permission(
            user=request.user,
            permission_key='employee.face_enrollment',
            business=biz,
            target_employee=employee
        )
    return False


class BiometricModelManifestView(views.APIView):
    """
    Returns official biometric model contract and threshold manifest.
    Mobile and backend read the same manifest to guarantee contract synchronization.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        manifest = ModelRegistry.get_manifest_dict()
        integrity_ok, msg = ModelRegistry.verify_recognition_model_integrity()
        manifest['integrity_status'] = 'VERIFIED' if integrity_ok else 'FAILED'
        manifest['integrity_message'] = msg
        return Response(manifest)


class EmployeeBiometricStatusView(views.APIView):
    """
    Returns current biometric enrollment & device authorization status for an employee.
    Does NOT return any raw embeddings or photos.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, employee_id):
        employee = get_object_or_404(Employee, id=employee_id)
        active_enrollment = EmployeeFaceEnrollment.objects.filter(
            employee=employee,
            status=BiometricStatus.ACTIVE
        ).first()

        active_device = EmployeeDevice.objects.filter(
            employee=employee,
            status=DeviceStatus.ACTIVE
        ).first()

        return Response({
            'employee_id': str(employee.id),
            'employee_name': employee.full_name,
            'status': active_enrollment.status if active_enrollment else 'NOT_ENROLLED',
            'model_id': active_enrollment.model_id if active_enrollment else ModelRegistry.FACE_MODEL_ID,
            'model_version': active_enrollment.model_version if active_enrollment else ModelRegistry.FACE_MODEL_VERSION,
            'quality_score': active_enrollment.quality_score if active_enrollment else None,
            'enrolled_at': active_enrollment.enrolled_at.isoformat() if active_enrollment else None,
            'enrolled_by': active_enrollment.enrolled_by.get_full_name() if active_enrollment and active_enrollment.enrolled_by else '—',
            'device_id': active_device.device_id if active_device else None,
            'device_name': active_device.device_name if active_device else None,
            'device_platform': active_device.platform if active_device else None,
            'device_status': active_device.status if active_device else 'NO_DEVICE',
        })


class EmployeeFaceEnrollView(views.APIView):
    """
    Enrolls or re-enrolls an employee's face template.
    Requires SuperAdmin, Business Admin, or Manager with 'employee.face_enrollment' permission.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, employee_id):
        employee = get_object_or_404(Employee, id=employee_id)
        if not _can_enroll_face(request, employee):
            return Response(
                {'detail': 'You do not have permission to enroll or re-enroll biometric faces.'},
                status=status.HTTP_403_FORBIDDEN
            )

        image_file = request.FILES.get('image')
        if not image_file:
            # Check for base64 image or raw bytes
            return Response({'detail': 'An image file is required for face enrollment.'}, status=status.HTTP_400_BAD_REQUEST)

        image_bytes = image_file.read()
        ip_addr = _get_client_ip(request)
        ua = request.META.get('HTTP_USER_AGENT', '')

        try:
            enrollment = EnrollmentService.enroll_employee_face(
                employee=employee,
                image_bytes=image_bytes,
                actor=request.user,
                ip_address=ip_addr,
                user_agent=ua
            )
            return Response({
                'detail': f'Face enrollment successfully created for {employee.full_name}.',
                'enrollment': EmployeeFaceEnrollmentSerializer(enrollment).data
            }, status=status.HTTP_201_CREATED)
        except ValidationError as e:
            return Response(getattr(e, 'message_dict', {'detail': str(e)}), status=status.HTTP_400_BAD_REQUEST)


class EmployeeFaceRevokeView(views.APIView):
    """
    Revokes an active face enrollment.
    Requires SuperAdmin, Business Admin, or Manager with 'employee.face_enrollment' permission.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, employee_id):
        employee = get_object_or_404(Employee, id=employee_id)
        if not _can_enroll_face(request, employee):
            return Response(
                {'detail': 'You do not have permission to revoke biometric faces.'},
                status=status.HTTP_403_FORBIDDEN
            )

        reason = request.data.get('reason', 'Revoked by administrator')
        ip_addr = _get_client_ip(request)
        ua = request.META.get('HTTP_USER_AGENT', '')

        success = EnrollmentService.revoke_employee_face(
            employee=employee,
            actor=request.user,
            reason=reason,
            ip_address=ip_addr,
            user_agent=ua
        )
        if not success:
            return Response({'detail': 'No active face enrollment found to revoke.'}, status=status.HTTP_404_NOT_FOUND)

        return Response({'detail': f'Face enrollment for {employee.full_name} has been revoked.'})


class DeviceRegisterView(views.APIView):
    """
    Registers an authorized mobile device for the authenticated employee.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ctx = get_user_context(request)
        emp = ctx.get('employee')
        if not emp:
            return Response({'detail': 'No linked employee profile found.'}, status=status.HTTP_400_BAD_REQUEST)

        serializer = DeviceRegistrationInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        device = DeviceService.register_device(
            employee=emp,
            device_id=serializer.validated_data['device_id'],
            device_name=serializer.validated_data.get('device_name', ''),
            platform=serializer.validated_data.get('platform', 'ANDROID'),
            public_key=serializer.validated_data.get('public_key', ''),
            actor=request.user,
            ip_address=_get_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')
        )
        return Response({
            'detail': 'Device successfully registered and authorized.',
            'device': EmployeeDeviceSerializer(device).data
        }, status=status.HTTP_201_CREATED)


class DeviceRevokeView(views.APIView):
    """
    Revokes authorization for a registered device.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ctx = get_user_context(request)
        emp = ctx.get('employee')
        if not emp:
            return Response({'detail': 'No linked employee profile found.'}, status=status.HTTP_400_BAD_REQUEST)

        serializer = RevokeDeviceInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        success = DeviceService.revoke_device(
            employee=emp,
            device_id=serializer.validated_data['device_id'],
            reason=serializer.validated_data.get('reason', 'User requested revocation'),
            actor=request.user,
            ip_address=_get_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', '')
        )
        if not success:
            return Response({'detail': 'Active device not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'detail': 'Device authorization revoked.'})


class BiometricTemplateProvisionView(views.APIView):
    """
    Provisions the employee's encrypted biometric template to their authorized device.
    Only the authorized active device for that employee receives the template.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        emp = ctx.get('employee')
        if not emp:
            return Response({'detail': 'No linked employee profile found.'}, status=status.HTTP_400_BAD_REQUEST)

        device_id = request.query_params.get('device_id') or request.headers.get('X-Device-Id')
        if not device_id:
            return Response({'detail': 'device_id header or parameter is required.'}, status=status.HTTP_400_BAD_REQUEST)

        device = EmployeeDevice.objects.filter(
            employee=emp,
            device_id=device_id,
            status=DeviceStatus.ACTIVE
        ).first()

        if not device:
            return Response(
                {'detail': 'Device is not registered or authorized for this employee.'},
                status=status.HTTP_403_FORBIDDEN
            )

        enrollment = EmployeeFaceEnrollment.objects.filter(
            employee=emp,
            status=BiometricStatus.ACTIVE,
            model_id=ModelRegistry.FACE_MODEL_ID
        ).first()

        if not enrollment:
            return Response(
                {'detail': 'Employee does not have an active face enrollment.'},
                status=status.HTTP_404_NOT_FOUND
            )

        payload = TemplateService.prepare_device_template_payload(enrollment, device)
        return Response(payload)


class BiometricChallengeView(views.APIView):
    """
    Issues a short-lived cryptographic nonce challenge (60s TTL) for biometric verification.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ctx = get_user_context(request)
        emp = ctx.get('employee')
        if not emp:
            return Response({'detail': 'No linked employee profile found.'}, status=status.HTTP_400_BAD_REQUEST)

        device_id = request.data.get('device_id') or ''
        challenge_data = VerificationService.create_challenge(emp, device_id=device_id)
        return Response(challenge_data)


class BiometricAuditListView(views.APIView):
    """
    Returns biometric audit logs for business. Requires SuperAdmin, Business Admin, or Manager.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        if ctx.get('role') not in [BusinessRole.SUPERADMIN, BusinessRole.BUSINESS_ADMIN, BusinessRole.MANAGER] and not request.user.is_superuser:
            return Response({'detail': 'Forbidden.'}, status=status.HTTP_403_FORBIDDEN)

        biz = ctx.get('business')
        qs = BiometricAuditEvent.objects.all()
        if biz and not request.user.is_superuser:
            qs = qs.filter(business=biz)

        emp_id = request.query_params.get('employee_id')
        if emp_id:
            qs = qs.filter(employee_id=emp_id)

        qs = qs[:100]
        return Response(BiometricAuditEventSerializer(qs, many=True).data)
