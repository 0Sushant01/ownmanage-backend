import re
from datetime import date, timedelta
from decimal import Decimal
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status
from rest_framework.exceptions import ValidationError

from apps.accounts.models import User, UserOTP, UserOTPPurpose
from apps.accounts.services import generate_and_send_otp, verify_and_consume_otp
from apps.organization.models import (
    Business, BusinessMembership, BusinessRole,
    Branch, Department, Employee, EmploymentStatus
)
from apps.subscriptions.models import (
    Plan, Subscription, SubscriptionStatus, SubscriptionHistory,
    CentreCapacityAllocation, Broker, Referral, Commission, CommissionStatus
)
from apps.subscriptions.services import allocate_centre_capacity, change_subscription_plan
from apps.leaves.models import LeaveType, LeaveRequest, LeaveRequestStatus, LeaveDuration


class OwnManagePlatformTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        today = date.today()

        # Commercial Plans
        self.starter_plan = Plan.objects.create(
            name='Starter Plan',
            max_centres=2,
            total_employee_capacity=10,
            monthly_charge=Decimal('999.00'),
            is_active=True
        )
        self.pro_plan = Plan.objects.create(
            name='Pro Plan',
            max_centres=5,
            total_employee_capacity=50,
            monthly_charge=Decimal('2499.00'),
            is_active=True
        )

        # Enterprise A
        self.biz = Business.objects.create(
            name='Acme Testing Corp',
            timezone='Asia/Kolkata',
            currency='INR',
            employee_id_enabled=True,
            employee_id_prefix='ACM',
            employee_id_next_number=1,
        )
        self.subscription = Subscription.objects.create(
            business=self.biz,
            plan=self.starter_plan,
            status=SubscriptionStatus.ACTIVE,
            start_date=today,
            current_period_start=today,
            current_period_end=today + timedelta(days=30)
        )

        # SuperAdmin
        self.superadmin = User.objects.create_superuser(
            email='platform_super@ownmanage.in',
            password='TestSuperPassword@123',
            first_name='Super',
            last_name='Admin'
        )

        # Broker User & Profile
        self.broker_user = User.objects.create_user(
            email='partner@brokerfirm.com',
            password='TestBrokerPassword@123',
            first_name='Broker',
            last_name='Partner'
        )
        self.broker = Broker.objects.create(
            user=self.broker_user,
            name='Alpha Brokerage',
            referral_code='ALPHA2026',
            commission_rate=Decimal('10.00'),
            is_active=True
        )

        # Business Admin
        self.admin_user = User.objects.create_user(
            email='admin@acmetest.com',
            password='TestAdminPassword@123',
            first_name='Admin',
            last_name='Acme'
        )
        BusinessMembership.objects.create(
            business=self.biz,
            user=self.admin_user,
            role=BusinessRole.BUSINESS_ADMIN
        )

        # Manager
        self.manager_user = User.objects.create_user(
            email='manager@acmetest.com',
            password='TestMgrPassword@123',
            first_name='Manager',
            last_name='Acme'
        )
        BusinessMembership.objects.create(
            business=self.biz,
            user=self.manager_user,
            role=BusinessRole.MANAGER
        )
        self.manager_emp = Employee.objects.create(
            business=self.biz,
            user=self.manager_user,
            employee_id='ACM001',
            first_name='Manager',
            last_name='Acme',
            email='manager@acmetest.com',
            designation='Branch Manager',
            joining_date=date(2025, 1, 1)
        )

        self.dept = Department.objects.create(business=self.biz, name='Operations', code='OPS')
        self.leave_type = LeaveType.objects.create(business=self.biz, name='Casual Leave', code='CL')

    # =========================================================================
    # 1. OTP Activation & Password Reset
    # =========================================================================
    def test_otp_generation_and_account_activation(self):
        new_user = User.objects.create_user(
            email='pending_staff@acmetest.com',
            first_name='Pending',
            last_name='Staff',
            is_active=False
        )

        result = generate_and_send_otp(new_user, UserOTPPurpose.ACTIVATION)
        self.assertIn('cooldown_seconds', result)

        # Extract 6-digit OTP from Django mail outbox
        self.assertEqual(len(mail.outbox), 1)
        match = re.search(r'\b\d{6}\b', mail.outbox[0].body)
        self.assertIsNotNone(match)
        raw_otp = match.group(0)
        self.assertEqual(len(raw_otp), 6)

        # Test invalid OTP raises ValidationError
        with self.assertRaises(ValidationError):
            verify_and_consume_otp(new_user, '000000', UserOTPPurpose.ACTIVATION)

        # Test activate account endpoint
        response = self.client.post(reverse('api-activate-account'), {
            'email': new_user.email,
            'otp': raw_otp,
            'new_password': 'BrandNewPassword@123'
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        new_user.refresh_from_db()
        self.assertTrue(new_user.is_active)
        self.assertTrue(new_user.check_password('BrandNewPassword@123'))

    def test_otp_max_attempts_lockout(self):
        user = User.objects.create_user(email='lockout@acmetest.com', password='Password@123')
        generate_and_send_otp(user, UserOTPPurpose.PASSWORD_RESET)

        # Extract OTP
        match = re.search(r'\b\d{6}\b', mail.outbox[-1].body)
        raw_otp = match.group(0)

        # 3 wrong attempts
        for _ in range(3):
            try:
                verify_and_consume_otp(user, '999999', UserOTPPurpose.PASSWORD_RESET)
            except ValidationError:
                pass

        otp_record = UserOTP.objects.filter(user=user, purpose=UserOTPPurpose.PASSWORD_RESET).first()
        self.assertGreaterEqual(otp_record.attempts, 3)

        # Even with correct OTP, it must now fail with ValidationError
        with self.assertRaises(ValidationError) as ctx:
            verify_and_consume_otp(user, raw_otp, UserOTPPurpose.PASSWORD_RESET)
        self.assertIn('Maximum verification attempts exceeded', str(ctx.exception))

    # =========================================================================
    # 2. Broker Role Isolation
    # =========================================================================
    def test_broker_auth_payload_and_dashboard(self):
        # Login as broker
        res = self.client.post(reverse('api-login'), {
            'email': self.broker_user.email,
            'password': 'TestBrokerPassword@123'
        })
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['role'], 'BROKER')

        token = res.data['tokens']['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # Broker Dashboard access
        dash_res = self.client.get(reverse('api-broker-dashboard'))
        self.assertEqual(dash_res.status_code, status.HTTP_200_OK)
        self.assertIn('total_referred_businesses', dash_res.data['metrics'])

        # Broker MUST NOT access enterprise operational endpoints (RBAC)
        emp_res = self.client.get(reverse('api-employees'))
        self.assertEqual(emp_res.status_code, status.HTTP_403_FORBIDDEN)

        att_res = self.client.get(reverse('api-attendance-today'))
        self.assertEqual(att_res.status_code, status.HTTP_403_FORBIDDEN)

        pay_res = self.client.get(reverse('api-salary-payrolls'))
        self.assertEqual(pay_res.status_code, status.HTTP_403_FORBIDDEN)

    # =========================================================================
    # 3. Centre Creation Limit (max_centres)
    # =========================================================================
    def test_plan_centre_limit_enforcement(self):
        # Login as Business Admin
        res = self.client.post(reverse('api-login'), {
            'email': self.admin_user.email,
            'password': 'TestAdminPassword@123'
        })
        token = res.data['tokens']['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # Starter Plan max_centres = 2
        # Centre 1
        c1 = self.client.post(reverse('api-centres'), {
            'name': 'North Centre',
            'code': 'NC1'
        })
        self.assertEqual(c1.status_code, status.HTTP_201_CREATED)

        # Centre 2
        c2 = self.client.post(reverse('api-centres'), {
            'name': 'South Centre',
            'code': 'SC1'
        })
        self.assertEqual(c2.status_code, status.HTTP_201_CREATED)

        # Centre 3 -> Should exceed plan limit (max_centres=2)
        c3 = self.client.post(reverse('api-centres'), {
            'name': 'East Centre',
            'code': 'EC1'
        })
        self.assertEqual(c3.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(c3.data.get('code'), 'PLAN_CENTRE_LIMIT_EXCEEDED')

    # =========================================================================
    # 4. Flexible Capacity Allocation & Employee Capacity Enforcement
    # =========================================================================
    def test_centre_capacity_allocation_and_employee_limit(self):
        # Create centre
        centre = Branch.objects.create(business=self.biz, name='Central Hub', code='CH1')

        # Starter plan has total_employee_capacity = 10
        # Allocate 2 seats to Central Hub
        alloc = allocate_centre_capacity(self.subscription, centre, new_capacity=2)
        self.assertEqual(alloc.allocated_capacity, 2)

        # Attempt to allocate 15 seats -> exceeds total_employee_capacity (10)
        with self.assertRaises(ValidationError):
            allocate_centre_capacity(self.subscription, centre, new_capacity=15)

        # Login as Admin
        res = self.client.post(reverse('api-login'), {
            'email': self.admin_user.email,
            'password': 'TestAdminPassword@123'
        })
        token = res.data['tokens']['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # Create employee 1 in centre
        e1 = self.client.post(reverse('api-employees'), {
            'first_name': 'Seat',
            'last_name': 'One',
            'email': 'seat1@acmetest.com',
            'branch_id': str(centre.id),
            'department_id': str(self.dept.id),
            'designation': 'Clerk',
            'joining_date': '2025-01-01'
        }, format='json')
        self.assertEqual(e1.status_code, status.HTTP_201_CREATED)

        # Create employee 2 in centre
        e2 = self.client.post(reverse('api-employees'), {
            'first_name': 'Seat',
            'last_name': 'Two',
            'email': 'seat2@acmetest.com',
            'branch_id': str(centre.id),
            'department_id': str(self.dept.id),
            'designation': 'Clerk',
            'joining_date': '2025-01-01'
        }, format='json')
        self.assertEqual(e2.status_code, status.HTTP_201_CREATED)

        # Create employee 3 in centre -> Exceeds allocated_capacity (2)
        e3 = self.client.post(reverse('api-employees'), {
            'first_name': 'Seat',
            'last_name': 'Three',
            'email': 'seat3@acmetest.com',
            'branch_id': str(centre.id),
            'department_id': str(self.dept.id),
            'designation': 'Clerk',
            'joining_date': '2025-01-01'
        }, format='json')
        self.assertEqual(e3.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(e3.data.get('code'), 'EMPLOYEE_CAPACITY_EXCEEDED')

    # =========================================================================
    # 5. Non-destructive Subscription Plan Transitions
    # =========================================================================
    def test_non_destructive_plan_upgrade_and_downgrade(self):
        # Initial subscription is Starter
        self.assertEqual(self.biz.subscription.plan.name, 'Starter Plan')

        # Upgrade to Pro
        new_sub = change_subscription_plan(self.biz, self.pro_plan)
        self.assertEqual(new_sub.plan.name, 'Pro Plan')

        # Downgrade back to Starter
        down_sub = change_subscription_plan(self.biz, self.starter_plan)
        self.assertEqual(down_sub.plan.name, 'Starter Plan')

        # Check history contains both records
        history = list(SubscriptionHistory.objects.filter(business=self.biz).order_by('created_at'))
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].plan.name, 'Pro Plan')
        self.assertEqual(history[0].action, 'UPGRADE')
        self.assertEqual(history[1].plan.name, 'Starter Plan')
        self.assertEqual(history[1].action, 'DOWNGRADE')

    # =========================================================================
    # 6. Manager Self-Approval Prohibition on Leave Requests
    # =========================================================================
    def test_manager_self_approval_forbidden(self):
        # Create leave request for Manager
        leave_req = LeaveRequest.objects.create(
            business=self.biz,
            employee=self.manager_emp,
            leave_type=self.leave_type,
            start_date=date(2025, 6, 1),
            end_date=date(2025, 6, 2),
            duration_type=LeaveDuration.FULL_DAY,
            reason='Personal trip',
            status=LeaveRequestStatus.PENDING
        )

        # Login as Manager
        res = self.client.post(reverse('api-login'), {
            'email': self.manager_user.email,
            'password': 'TestMgrPassword@123'
        })
        token = res.data['tokens']['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

        # Manager attempts to approve own leave
        approve_res = self.client.post(reverse('api-leave-approve', kwargs={'pk': leave_req.id}), {
            'review_notes': 'Approving my own leave'
        })
        self.assertEqual(approve_res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('cannot approve your own leave request', str(approve_res.data))

        # Business Admin logs in and approves it
        res_admin = self.client.post(reverse('api-login'), {
            'email': self.admin_user.email,
            'password': 'TestAdminPassword@123'
        })
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {res_admin.data["tokens"]["access"]}')

        approve_admin = self.client.post(reverse('api-leave-approve', kwargs={'pk': leave_req.id}), {
            'review_notes': 'Admin approved'
        })
        self.assertEqual(approve_admin.status_code, status.HTTP_200_OK)
        leave_req.refresh_from_db()
        self.assertEqual(leave_req.status, LeaveRequestStatus.APPROVED)
