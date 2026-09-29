from datetime import date
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status

from apps.accounts.models import User
from apps.organization.models import Business, BusinessMembership, BusinessRole, Branch
from apps.subscriptions.models import (
    Plan, Subscription, SubscriptionHistory, Broker, Referral, Commission, SubscriptionPayment
)


class SuperAdminUpgradesTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        # 1. SuperAdmin User
        self.superadmin = User.objects.create_superuser(
            email='superadmin@ownmanage.in',
            password='Password@123',
            first_name='Global',
            last_name='Admin'
        )

        # 2. Plan
        self.plan = Plan.objects.create(
            name='Professional Tier',
            monthly_charge=12999.00,
            max_centres=5,
            total_employee_capacity=300
        )

        # 3. Business & Subscription
        self.business = Business.objects.create(
            name='Acme Global',
            email='info@acme.com',
            timezone='Asia/Kolkata'
        )
        self.centre = Branch.objects.create(
            business=self.business,
            name='Main Campus',
            code='MC01'
        )
        self.subscription = Subscription.objects.create(
            business=self.business,
            plan=self.plan,
            status='ACTIVE',
            start_date=date.today(),
            current_period_start=date.today(),
            current_period_end=date.today()
        )

        # 4. Broker
        self.broker_user = User.objects.create_user(
            email='broker@partner.com',
            password='Password@123',
            first_name='Broker'
        )
        self.broker = Broker.objects.create(
            user=self.broker_user,
            name='Partner Advisors',
            referral_code='ADVISOR2026',
            commission_rate=12.50
        )
        self.referral = Referral.objects.create(
            broker=self.broker,
            business=self.business,
            referral_code_used='ADVISOR2026'
        )
        self.commission = Commission.objects.create(
            broker=self.broker,
            business=self.business,
            subscription=self.subscription,
            plan=self.plan,
            period_start=date.today(),
            period_end=date.today(),
            commission_amount=1624.87,
            status='PENDING'
        )

    def test_plan_subscribers_endpoint(self):
        self.client.force_authenticate(user=self.superadmin)
        res = self.client.get(f'/api/v1/plans/{self.plan.id}/subscribers/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertIn('subscribers', data)
        self.assertEqual(len(data['subscribers']), 1)
        self.assertEqual(data['subscribers'][0]['business_name'], 'Acme Global')
        self.assertEqual(data['subscribers'][0]['broker_name'], 'Partner Advisors')

    def test_broker_detail_and_commission_payout(self):
        self.client.force_authenticate(user=self.superadmin)
        res = self.client.get(f'/api/v1/brokers/{self.broker.id}/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertEqual(data['broker']['referral_code'], 'ADVISOR2026')
        self.assertEqual(len(data['referred_businesses']), 1)

        # Pay commission
        pay_res = self.client.post(f'/api/v1/commissions/{self.commission.id}/pay/', {
            'payment_reference': 'TXN_NEFT_998877',
            'notes': 'Settled for September cycle'
        })
        self.assertEqual(pay_res.status_code, status.HTTP_200_OK)
        self.commission.refresh_from_db()
        self.assertEqual(self.commission.status, 'PAID')
        self.assertEqual(self.commission.payment_reference, 'TXN_NEFT_998877')

    def test_change_password_validation_and_audit(self):
        self.client.force_authenticate(user=self.superadmin)
        # 1. Invalid current password
        bad_cur = self.client.post('/api/v1/auth/change-password/', {
            'current_password': 'WrongPassword',
            'new_password': 'NewPassword@123',
            'confirm_password': 'NewPassword@123'
        })
        self.assertEqual(bad_cur.status_code, status.HTTP_400_BAD_REQUEST)

        # 2. Weak new password (no special char)
        weak_pwd = self.client.post('/api/v1/auth/change-password/', {
            'current_password': 'Password@123',
            'new_password': 'NewPassword123',
            'confirm_password': 'NewPassword123'
        })
        self.assertEqual(weak_pwd.status_code, status.HTTP_400_BAD_REQUEST)

        # 3. Successful strong password change
        good_pwd = self.client.post('/api/v1/auth/change-password/', {
            'current_password': 'Password@123',
            'new_password': 'NewStrongPassword@2026',
            'confirm_password': 'NewStrongPassword@2026'
        })
        self.assertEqual(good_pwd.status_code, status.HTTP_200_OK)
        self.superadmin.refresh_from_db()
        self.assertTrue(self.superadmin.check_password('NewStrongPassword@2026'))

        # Verify audit log recorded
        audit_res = self.client.get('/api/v1/audit-logs/?action=PASSWORD_CHANGED')
        self.assertEqual(audit_res.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(audit_res.json()), 1)

    def test_business_centres_capacity(self):
        self.client.force_authenticate(user=self.superadmin)
        res = self.client.get(f'/api/v1/businesses/{self.business.id}/centres/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        centres = res.json()
        self.assertEqual(len(centres), 1)
        self.assertEqual(centres[0]['code'], 'MC01')
