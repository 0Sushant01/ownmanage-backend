from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.serializers import LoginSerializer, UserSerializer, ProfileUpdateSerializer
from apps.core.permissions import get_user_context
from apps.organization.models import BusinessMembership, Employee, BusinessRole


def build_user_payload(user, context=None):
    """
    Constructs the serialized payload containing authentication info,
    active tenant, role, and linked employee details.
    """
    memberships_qs = BusinessMembership.objects.filter(
        user=user,
        is_active=True,
        business__is_active=True
    ).select_related('business')

    memberships_data = []
    for m in memberships_qs:
        memberships_data.append({
            'business_id': str(m.business.id),
            'business_name': m.business.name,
            'role': m.role,
        })

    active_role = None
    active_business = None
    active_employee = None

    if user.is_superuser:
        active_role = BusinessRole.SUPERADMIN
        if context and context.get('business'):
            b = context['business']
            active_business = {'id': str(b.id), 'name': b.name}
    elif hasattr(user, 'broker_profile') and user.broker_profile.is_active:
        active_role = BusinessRole.BROKER
    elif memberships_data:
        m = memberships_qs.first()
        active_role = m.role
        active_business = {'id': str(m.business.id), 'name': m.business.name}
        emp = Employee.objects.filter(business=m.business, user=user).first()
        if emp:
            active_employee = {
                'id': str(emp.id),
                'employee_id': emp.employee_id,
                'designation': emp.designation,
                'department': emp.department.name if emp.department else None,
                'branch': emp.branch.name if emp.branch else None,
            }

    return {
        'user': UserSerializer(user).data,
        'role': active_role,
        'business': active_business,
        'employee': active_employee,
        'memberships': memberships_data,
    }


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']

        refresh = RefreshToken.for_user(user)
        user_payload = build_user_payload(user)

        return Response({
            'tokens': {
                'access': str(refresh.access_token),
                'refresh': str(refresh),
            },
            **user_payload
        }, status=status.HTTP_200_OK)


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        # Allow client to invalidate refresh token if provided
        refresh_token = request.data.get('refresh')
        if refresh_token:
            try:
                token = RefreshToken(refresh_token)
                token.blacklist()
            except Exception:
                pass
        return Response({'detail': 'Successfully logged out.'}, status=status.HTTP_200_OK)


class CurrentUserView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        payload = build_user_payload(request.user, context=ctx)
        return Response(payload, status=status.HTTP_200_OK)


class ProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        user_data = UserSerializer(request.user).data
        emp_data = None
        if ctx.get('employee'):
            e = ctx['employee']
            emp_data = {
                'id': str(e.id),
                'employee_id': e.employee_id,
                'first_name': e.first_name,
                'last_name': e.last_name,
                'email': e.email,
                'phone': e.phone,
                'designation': e.designation,
                'joining_date': str(e.joining_date),
                'department': e.department.name if e.department else None,
                'branch': e.branch.name if e.branch else None,
                'manager': str(e.manager) if e.manager else None,
            }

        return Response({
            'user': user_data,
            'employee': emp_data,
            'role': ctx.get('role'),
            'business': {'id': str(ctx['business'].id), 'name': ctx['business'].name} if ctx.get('business') else None,
        }, status=status.HTTP_200_OK)

    def patch(self, request):
        serializer = ProfileUpdateSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserSerializer(request.user).data, status=status.HTTP_200_OK)


class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get('email', '').strip().lower()
        if not email:
            return Response({'detail': 'Email address is required.'}, status=status.HTTP_400_BAD_REQUEST)

        from apps.accounts.models import User, UserOTPPurpose
        from apps.accounts.services import generate_and_send_otp

        user = User.objects.filter(email=email).first()
        if not user:
            # Prevent user enumeration by returning generic success message
            return Response({
                'detail': 'If an account exists with this email, a verification code has been dispatched.',
                'cooldown_seconds': 60
            }, status=status.HTTP_200_OK)

        res = generate_and_send_otp(user, UserOTPPurpose.PASSWORD_RESET)
        return Response(res, status=status.HTTP_200_OK)


class VerifyOTPView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get('email', '').strip().lower()
        otp = request.data.get('otp', '').strip()
        purpose = request.data.get('purpose', 'PASSWORD_RESET')

        if not email or not otp:
            return Response({'detail': 'Email and OTP code are required.'}, status=status.HTTP_400_BAD_REQUEST)

        from apps.accounts.models import User
        from apps.accounts.services import verify_and_consume_otp

        user = User.objects.filter(email=email).first()
        if not user:
            return Response({'detail': 'Invalid email address.'}, status=status.HTTP_400_BAD_REQUEST)

        verify_and_consume_otp(user, otp, purpose)
        return Response({'detail': 'Verification code successfully validated.'}, status=status.HTTP_200_OK)


class ResetPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get('email', '').strip().lower()
        otp = request.data.get('otp', '').strip()
        new_password = request.data.get('password') or request.data.get('new_password', '')

        if not email or not otp or not new_password:
            return Response({'detail': 'Email, verification code, and new password are required.'}, status=status.HTTP_400_BAD_REQUEST)

        if len(new_password) < 8:
            return Response({'detail': 'Password must be at least 8 characters long.'}, status=status.HTTP_400_BAD_REQUEST)

        from apps.accounts.models import User, UserOTPPurpose
        from apps.accounts.services import verify_and_consume_otp

        user = User.objects.filter(email=email).first()
        if not user:
            return Response({'detail': 'Invalid email address.'}, status=status.HTTP_400_BAD_REQUEST)

        verify_and_consume_otp(user, otp, UserOTPPurpose.PASSWORD_RESET)
        user.set_password(new_password)
        user.save(update_fields=['password'])

        return Response({'detail': 'Password successfully reset. You may now log in.'}, status=status.HTTP_200_OK)


class ActivateAccountView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get('email', '').strip().lower()
        otp = request.data.get('otp', '').strip()
        new_password = request.data.get('password') or request.data.get('new_password', '')

        if not email or not otp or not new_password:
            return Response({'detail': 'Email, activation code, and new password are required.'}, status=status.HTTP_400_BAD_REQUEST)

        if len(new_password) < 8:
            return Response({'detail': 'Password must be at least 8 characters long.'}, status=status.HTTP_400_BAD_REQUEST)

        from apps.accounts.models import User, UserOTPPurpose
        from apps.accounts.services import verify_and_consume_otp

        user = User.objects.filter(email=email).first()
        if not user:
            return Response({'detail': 'Invalid account.'}, status=status.HTTP_400_BAD_REQUEST)

        verify_and_consume_otp(user, otp, UserOTPPurpose.ACTIVATION)
        user.set_password(new_password)
        user.is_active = True
        user.save(update_fields=['password', 'is_active'])

        return Response({'detail': 'Account successfully activated. You may now log in.'}, status=status.HTTP_200_OK)

