from datetime import date, timedelta
from decimal import Decimal
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status

from apps.accounts.models import User
from apps.organization.models import Business, BusinessMembership, BusinessRole, Branch, Employee
from apps.subscriptions.models import (
    Plan, Subscription, SubscriptionStatus,
    Broker, Referral, SubscriptionPayment, PaymentStatus
)


class SuperAdminAnalyticsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.today = date.today()

        # 1. SuperAdmin User
        self.superadmin = User.objects.create_user(
            email='superadmin.test@ownmanage.in',
            password='testpassword123',
            first_name='Global',
            last_name='SuperAdmin',
            is_staff=True,
            is_superuser=True
        )

        # 2. Business Admin User
        self.biz_admin = User.objects.create_user(
            email='bizadmin.test@acme.com',
            password='testpassword123',
            first_name='Acme',
            last_name='Admin'
        )

        # 3. Staff User
        self.staff_user = User.objects.create_user(
            email='staff.test@acme.com',
            password='testpassword123',
            first_name='Staff',
            last_name='Member'
        )

        # 4. Broker User & Entity
        self.broker_user = User.objects.create_user(
            email='broker.test@advisor.in',
            password='testpassword123',
            first_name='Broker',
            last_name='Partner'
        )
        self.broker = Broker.objects.create(
            user=self.broker_user,
            name='Test Broker Partners',
            referral_code='TESTBROKER',
            commission_rate=Decimal('10.00'),
            is_active=True
        )

        # 5. Commercial Plans
        self.starter_plan = Plan.objects.create(
            name='Starter Test Plan',
            monthly_charge=Decimal('4999.00'),
            max_centres=2,
            total_employee_capacity=50,
            is_active=True
        )
        self.pro_plan = Plan.objects.create(
            name='Pro Test Plan',
            monthly_charge=Decimal('14999.00'),
            max_centres=5,
            total_employee_capacity=200,
            is_active=True
        )

        # 6. Business 1 (Active, Pro Plan, Paid)
        self.biz1 = Business.objects.create(
            name='Alpha Corp',
            is_active=True,
            timezone='Asia/Kolkata',
            currency='INR'
        )
        BusinessMembership.objects.create(business=self.biz1, user=self.biz_admin, role=BusinessRole.BUSINESS_ADMIN)
        BusinessMembership.objects.create(business=self.biz1, user=self.staff_user, role=BusinessRole.STAFF)
        Branch.objects.create(business=self.biz1, name='HQ', code='A-01')
        Employee.objects.create(business=self.biz1, user=self.staff_user, first_name='Staff', last_name='Member', joining_date=self.today)

        self.sub1 = Subscription.objects.create(
            business=self.biz1,
            plan=self.pro_plan,
            status=SubscriptionStatus.ACTIVE_PAID,
            start_date=self.today - timedelta(days=60),
            current_period_start=self.today.replace(day=1),
            current_period_end=self.today.replace(day=28)
        )
        Referral.objects.create(business=self.biz1, broker=self.broker, referral_code_used='TESTBROKER')

        SubscriptionPayment.objects.create(
            business=self.biz1,
            subscription=self.sub1,
            plan=self.pro_plan,
            amount=Decimal('14999.00'),
            billing_date=self.today.replace(day=1),
            due_date=self.today.replace(day=5),
            status=PaymentStatus.PAID
        )

        # 7. Business 2 (Active, Starter Plan, Overdue Payment)
        self.biz2 = Business.objects.create(
            name='Beta Solutions',
            is_active=True,
            timezone='Asia/Kolkata',
            currency='INR'
        )
        self.sub2 = Subscription.objects.create(
            business=self.biz2,
            plan=self.starter_plan,
            status=SubscriptionStatus.OVERDUE,
            start_date=self.today - timedelta(days=30),
            current_period_start=self.today.replace(day=1),
            current_period_end=self.today.replace(day=28)
        )
        SubscriptionPayment.objects.create(
            business=self.biz2,
            subscription=self.sub2,
            plan=self.starter_plan,
            amount=Decimal('4999.00'),
            billing_date=self.today.replace(day=1),
            due_date=self.today.replace(day=5),
            status=PaymentStatus.OVERDUE
        )

        self.analytics_url = reverse('api-superadmin-analytics')

    def test_superadmin_analytics_success(self):
        self.client.force_authenticate(user=self.superadmin)
        response = self.client.get(self.analytics_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data

        # Verify Top-Level Structure
        self.assertIn('period', data)
        self.assertIn('kpis', data)
        self.assertIn('growth_chart', data)
        self.assertIn('revenue_chart', data)
        self.assertIn('status_breakdown', data)
        self.assertIn('plan_distribution', data)
        self.assertIn('broker_performance', data)
        self.assertIn('action_required', data)
        self.assertIn('recent_activity', data)

        # Verify KPIs calculated accurately
        kpis = data['kpis']
        self.assertEqual(kpis['total_businesses'], 2)
        self.assertEqual(kpis['active_businesses'], 2)
        self.assertEqual(kpis['total_employees'], 1)
        self.assertEqual(kpis['subscription_revenue'], 14999.0)
        self.assertEqual(kpis['paid_subscriptions'], 1)
        self.assertEqual(kpis['payment_due_count'], 1)
        self.assertEqual(kpis['payment_due_amount'], 4999.0)

        # Verify charts have 6 monthly intervals
        self.assertEqual(len(data['growth_chart']), 6)
        self.assertEqual(len(data['revenue_chart']), 6)

    def test_superadmin_analytics_rbac_forbidden(self):
        # Business Admin cannot access
        self.client.force_authenticate(user=self.biz_admin)
        res = self.client.get(self.analytics_url)
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        # Staff cannot access
        self.client.force_authenticate(user=self.staff_user)
        res = self.client.get(self.analytics_url)
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        # Broker partner cannot access
        self.client.force_authenticate(user=self.broker_user)
        res = self.client.get(self.analytics_url)
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        # Anonymous cannot access
        self.client.logout()
        res = self.client.get(self.analytics_url)
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_superadmin_analytics_filter_plan(self):
        self.client.force_authenticate(user=self.superadmin)
        # Filter for Starter Plan only
        response = self.client.get(self.analytics_url, {'plan_id': str(self.starter_plan.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        kpis = response.data['kpis']
        self.assertEqual(kpis['total_businesses'], 1)
        self.assertEqual(kpis['subscription_revenue'], 0.0)
        self.assertEqual(kpis['payment_due_count'], 1)
        self.assertEqual(kpis['payment_due_amount'], 4999.0)
