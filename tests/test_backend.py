from datetime import date, timedelta
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status

from apps.accounts.models import User
from apps.organization.models import (
    Business, BusinessMembership, BusinessRole,
    Department, Branch, Employee, EmploymentStatus
)
from apps.organization.services import generate_next_employee_id
from apps.attendance.models import AttendanceDay, AttendanceEvent, AttendanceStatus, AttendanceEventType
from apps.leaves.models import LeaveType, LeaveRequest, LeaveRequestStatus
from apps.payroll.models import Payroll, PayrollStatus


class OwnManageTestSuite(TestCase):
    def setUp(self):
        self.client = APIClient()

        # Business A
        self.biz_a = Business.objects.create(
            name='Alpha Corp',
            timezone='Asia/Kolkata',
            currency='INR',
            employee_id_enabled=True,
            employee_id_prefix='ALP',
            employee_id_next_number=1,
        )
        self.dept_a = Department.objects.create(business=self.biz_a, name='Engineering', code='ENG')
        self.leave_cl_a = LeaveType.objects.create(business=self.biz_a, name='Casual Leave', code='CL')

        # Business B
        self.biz_b = Business.objects.create(
            name='Beta Solutions',
            timezone='Asia/Kolkata',
            currency='INR',
            employee_id_enabled=True,
            employee_id_prefix='BET',
            employee_id_next_number=1,
        )

        # 1. SuperAdmin
        self.superadmin = User.objects.create_superuser(
            email='super@ownmanage.in',
            password='TestPassword@123',
            first_name='Super',
            last_name='Admin'
        )

        # 2. Business Admin A
        self.admin_a = User.objects.create_user(
            email='admin_a@alpha.com',
            password='TestPassword@123',
            first_name='Admin',
            last_name='Alpha'
        )
        BusinessMembership.objects.create(business=self.biz_a, user=self.admin_a, role=BusinessRole.BUSINESS_ADMIN)

        # 3. Manager 1 (Business A)
        self.mgr1_user = User.objects.create_user(
            email='mgr1@alpha.com',
            password='TestPassword@123',
            first_name='Manager',
            last_name='One'
        )
        BusinessMembership.objects.create(business=self.biz_a, user=self.mgr1_user, role=BusinessRole.MANAGER)
        self.mgr1_emp = Employee.objects.create(
            business=self.biz_a, user=self.mgr1_user, employee_id='ALP001',
            first_name='Manager', last_name='One', email='mgr1@alpha.com',
            designation='Lead Manager', joining_date=date(2025, 1, 1)
        )

        # 4. Manager 2 (Business A)
        self.mgr2_user = User.objects.create_user(
            email='mgr2@alpha.com',
            password='TestPassword@123',
            first_name='Manager',
            last_name='Two'
        )
        BusinessMembership.objects.create(business=self.biz_a, user=self.mgr2_user, role=BusinessRole.MANAGER)
        self.mgr2_emp = Employee.objects.create(
            business=self.biz_a, user=self.mgr2_user, employee_id='ALP002',
            first_name='Manager', last_name='Two', email='mgr2@alpha.com',
            designation='Operations Lead', joining_date=date(2025, 1, 1)
        )

        # 5. Staff 1 (Assigned to Manager 1)
        self.staff1_user = User.objects.create_user(
            email='staff1@alpha.com',
            password='TestPassword@123',
            first_name='Staff',
            last_name='One'
        )
        BusinessMembership.objects.create(business=self.biz_a, user=self.staff1_user, role=BusinessRole.STAFF)
        self.staff1_emp = Employee.objects.create(
            business=self.biz_a, user=self.staff1_user, employee_id='ALP003',
            first_name='Staff', last_name='One', email='staff1@alpha.com',
            designation='Software Engineer', manager=self.mgr1_emp, joining_date=date(2025, 2, 1)
        )

        # 6. Staff 2 (Assigned to Manager 2)
        self.staff2_user = User.objects.create_user(
            email='staff2@alpha.com',
            password='TestPassword@123',
            first_name='Staff',
            last_name='Two'
        )
        BusinessMembership.objects.create(business=self.biz_a, user=self.staff2_user, role=BusinessRole.STAFF)
        self.staff2_emp = Employee.objects.create(
            business=self.biz_a, user=self.staff2_user, employee_id='ALP004',
            first_name='Staff', last_name='Two', email='staff2@alpha.com',
            designation='Associate', manager=self.mgr2_emp, joining_date=date(2025, 2, 1)
        )

        # 7. Employee in Business B
        self.staff_b_user = User.objects.create_user(
            email='staff_b@beta.com',
            password='TestPassword@123',
            first_name='Beta',
            last_name='Staff'
        )
        BusinessMembership.objects.create(business=self.biz_b, user=self.staff_b_user, role=BusinessRole.STAFF)
        self.staff_b_emp = Employee.objects.create(
            business=self.biz_b, user=self.staff_b_user, employee_id='BET001',
            first_name='Beta', last_name='Staff', email='staff_b@beta.com',
            designation='Engineer', joining_date=date(2025, 1, 1)
        )

    # =========================================================================
    # PART 29.1: Authentication Tests
    # =========================================================================
    def test_valid_login(self):
        url = reverse('api-login')
        response = self.client.post(url, {'email': 'admin_a@alpha.com', 'password': 'TestPassword@123'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('tokens', response.data)
        self.assertIn('access', response.data['tokens'])
        self.assertEqual(response.data['role'], BusinessRole.BUSINESS_ADMIN)
        self.assertEqual(response.data['business']['name'], 'Alpha Corp')

    def test_invalid_login_password(self):
        url = reverse('api-login')
        response = self.client.post(url, {'email': 'admin_a@alpha.com', 'password': 'WrongPassword'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_inactive_user_cannot_login(self):
        self.admin_a.is_active = False
        self.admin_a.save()
        url = reverse('api-login')
        response = self.client.post(url, {'email': 'admin_a@alpha.com', 'password': 'TestPassword@123'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('inactive', str(response.data).lower())

    # =========================================================================
    # PART 29.2: Tenant Isolation Tests (Business A cannot access Business B)
    # =========================================================================
    def test_tenant_isolation_business_admin_cannot_access_other_business(self):
        self.client.force_authenticate(user=self.admin_a)
        # Attempt to access Business B detail
        url = reverse('api-business-detail', kwargs={'pk': self.biz_b.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_tenant_isolation_employee_list(self):
        self.client.force_authenticate(user=self.admin_a)
        url = reverse('api-employees')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        returned_ids = [e['id'] for e in response.data]
        self.assertNotIn(str(self.staff_b_emp.id), returned_ids)

    # =========================================================================
    # PART 29.3: Manager Isolation Tests (Manager 1 cannot access Manager 2 staff)
    # =========================================================================
    def test_manager_only_sees_assigned_staff(self):
        self.client.force_authenticate(user=self.mgr1_user)
        url = reverse('api-employees')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        returned_ids = [e['id'] for e in response.data]
        # Staff 1 is assigned to Manager 1
        self.assertIn(str(self.staff1_emp.id), returned_ids)
        # Staff 2 is assigned to Manager 2 -> must NOT be present
        self.assertNotIn(str(self.staff2_emp.id), returned_ids)

    def test_manager_cannot_view_unassigned_staff_detail(self):
        self.client.force_authenticate(user=self.mgr1_user)
        url = reverse('api-employee-detail', kwargs={'pk': self.staff2_emp.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # =========================================================================
    # PART 29.4: Staff Isolation Tests (Staff A cannot access Staff B)
    # =========================================================================
    def test_staff_cannot_access_other_staff_detail(self):
        self.client.force_authenticate(user=self.staff1_user)
        url = reverse('api-employee-detail', kwargs={'pk': self.staff2_emp.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_only_sees_self_in_list(self):
        self.client.force_authenticate(user=self.staff1_user)
        url = reverse('api-employees')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['id'], str(self.staff1_emp.id))

    # =========================================================================
    # PART 29.5: Attendance Tests (check-in, check-out, duplicate/invalid prevention)
    # =========================================================================
    def test_attendance_check_in_and_check_out_flow(self):
        self.client.force_authenticate(user=self.staff1_user)

        # 1. Check in
        check_in_url = reverse('api-attendance-checkin')
        in_resp = self.client.post(check_in_url, {'source': 'MOBILE'}, format='json')
        self.assertEqual(in_resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(in_resp.data['is_checked_in'])

        # 2. Duplicate check-in should fail
        dup_resp = self.client.post(check_in_url, {'source': 'MOBILE'}, format='json')
        self.assertEqual(dup_resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Already checked in', str(dup_resp.data))

        # 3. Check out
        check_out_url = reverse('api-attendance-checkout')
        out_resp = self.client.post(check_out_url, {'source': 'MOBILE'}, format='json')
        self.assertEqual(out_resp.status_code, status.HTTP_200_OK)
        self.assertFalse(out_resp.data['is_checked_in'])

        # 4. Check out again without new check-in should fail
        dup_out_resp = self.client.post(check_out_url, {'source': 'MOBILE'}, format='json')
        self.assertEqual(dup_out_resp.status_code, status.HTTP_400_BAD_REQUEST)

    # =========================================================================
    # PART 29.6: Leave Management Tests
    # =========================================================================
    def test_leave_apply_manager_approve_and_isolation(self):
        # 1. Staff 1 applies for leave
        self.client.force_authenticate(user=self.staff1_user)
        apply_url = reverse('api-leaves')
        leave_data = {
            'leave_type': str(self.leave_cl_a.id),
            'start_date': str(date.today() + timedelta(days=2)),
            'end_date': str(date.today() + timedelta(days=3)),
            'reason': 'Vacation'
        }
        resp = self.client.post(apply_url, leave_data, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        leave_id = resp.data['id']
        self.assertEqual(resp.data['status'], LeaveRequestStatus.PENDING)

        # 2. Manager 2 (not their manager) attempts to approve -> should be forbidden
        self.client.force_authenticate(user=self.mgr2_user)
        approve_url = reverse('api-leave-approve', kwargs={'pk': leave_id})
        forbidden_resp = self.client.post(approve_url)
        self.assertEqual(forbidden_resp.status_code, status.HTTP_403_FORBIDDEN)

        # 3. Manager 1 (their direct manager) approves -> success
        self.client.force_authenticate(user=self.mgr1_user)
        approve_resp = self.client.post(approve_url)
        self.assertEqual(approve_resp.status_code, status.HTTP_200_OK)
        self.assertEqual(approve_resp.data['leave_request']['status'], LeaveRequestStatus.APPROVED)

    # =========================================================================
    # PART 29.7: Employee ID Concurrency & Sequence Tests
    # =========================================================================
    def test_employee_id_generation_sequential_and_unique(self):
        # In setUp, ALP001 to ALP004 were created. Next sequence generated must be ALP005, ALP006, ALP007.
        id1 = generate_next_employee_id(self.biz_a)
        id2 = generate_next_employee_id(self.biz_a)
        id3 = generate_next_employee_id(self.biz_a)

        self.assertEqual(id1, 'ALP005')
        self.assertEqual(id2, 'ALP006')
        self.assertEqual(id3, 'ALP007')

        # Check Business B sequence is independent
        # In setUp, BET001 was created. Next sequence generated must be BET002.
        id_b = generate_next_employee_id(self.biz_b)
        self.assertEqual(id_b, 'BET002')
