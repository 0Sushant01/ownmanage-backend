from rest_framework import serializers
from apps.subscriptions.models import (
    Plan, Subscription, SubscriptionHistory,
    CentreCapacityAllocation, Broker, Referral, Commission
)


class PlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plan
        fields = [
            'id', 'name', 'monthly_charge', 'max_centres',
            'total_employee_capacity', 'features', 'is_active',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class CentreCapacityAllocationSerializer(serializers.ModelSerializer):
    centre_name = serializers.CharField(source='centre.name', read_only=True)
    centre_code = serializers.CharField(source='centre.code', read_only=True)
    active_employees_count = serializers.IntegerField(source='centre.active_employees_count', read_only=True)

    class Meta:
        model = CentreCapacityAllocation
        fields = [
            'id', 'centre', 'centre_name', 'centre_code',
            'allocated_capacity', 'active_employees_count', 'updated_at'
        ]
        read_only_fields = ['id', 'updated_at']


class SubscriptionSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    monthly_charge = serializers.DecimalField(source='plan.monthly_charge', max_digits=12, decimal_places=2, read_only=True)
    max_centres = serializers.IntegerField(source='plan.max_centres', read_only=True)
    total_employee_capacity = serializers.IntegerField(source='plan.total_employee_capacity', read_only=True)
    features = serializers.JSONField(source='plan.features', read_only=True)
    centre_allocations = CentreCapacityAllocationSerializer(many=True, read_only=True)
    current_centres_count = serializers.SerializerMethodField()
    current_employees_count = serializers.SerializerMethodField()
    total_allocated_capacity = serializers.IntegerField(read_only=True)
    unallocated_capacity = serializers.IntegerField(read_only=True)

    class Meta:
        model = Subscription
        fields = [
            'id', 'business', 'plan', 'plan_name', 'monthly_charge',
            'max_centres', 'total_employee_capacity', 'features',
            'status', 'start_date', 'end_date', 'current_period_start', 'current_period_end',
            'current_centres_count', 'current_employees_count',
            'total_allocated_capacity', 'unallocated_capacity',
            'centre_allocations', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'business', 'created_at', 'updated_at']

    def get_current_centres_count(self, obj) -> int:
        return obj.business.branches.filter(is_active=True).count()

    def get_current_employees_count(self, obj) -> int:
        return obj.business.employees.filter(employment_status='ACTIVE').count()


class SubscriptionHistorySerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source='plan.name', read_only=True)

    class Meta:
        model = SubscriptionHistory
        fields = [
            'id', 'business', 'plan', 'plan_name', 'action',
            'monthly_charge', 'max_centres', 'total_employee_capacity',
            'effective_from', 'effective_to', 'reason', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class BrokerSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source='user.email', read_only=True)
    referred_businesses_count = serializers.SerializerMethodField()
    total_commission_earned = serializers.SerializerMethodField()
    pending_commission = serializers.SerializerMethodField()

    class Meta:
        model = Broker
        fields = [
            'id', 'user', 'email', 'name', 'referral_code', 'commission_rate',
            'is_active', 'referred_businesses_count', 'total_commission_earned',
            'pending_commission', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']

    def get_referred_businesses_count(self, obj) -> int:
        return obj.referrals.count()

    def get_total_commission_earned(self, obj) -> float:
        return float(sum(c.commission_amount for c in obj.commissions.filter(status='PAID')))

    def get_pending_commission(self, obj) -> float:
        return float(sum(c.commission_amount for c in obj.commissions.filter(status='PENDING')))


class ReferralSerializer(serializers.ModelSerializer):
    business_name = serializers.CharField(source='business.name', read_only=True)
    plan_name = serializers.SerializerMethodField()
    subscription_status = serializers.SerializerMethodField()
    centres_count = serializers.SerializerMethodField()
    employees_count = serializers.SerializerMethodField()

    class Meta:
        model = Referral
        fields = [
            'id', 'broker', 'business', 'business_name', 'referral_code_used',
            'plan_name', 'subscription_status', 'centres_count', 'employees_count',
            'referred_at'
        ]
        read_only_fields = ['id', 'referred_at']

    def get_plan_name(self, obj) -> str:
        if hasattr(obj.business, 'subscription') and obj.business.subscription:
            return obj.business.subscription.plan.name
        return 'No Active Plan'

    def get_subscription_status(self, obj) -> str:
        if hasattr(obj.business, 'subscription') and obj.business.subscription:
            return obj.business.subscription.status
        return 'INACTIVE'

    def get_centres_count(self, obj) -> int:
        return obj.business.branches.filter(is_active=True).count()

    def get_employees_count(self, obj) -> int:
        return obj.business.employees.filter(employment_status='ACTIVE').count()


class CommissionSerializer(serializers.ModelSerializer):
    broker_name = serializers.CharField(source='broker.name', read_only=True)
    business_name = serializers.CharField(source='business.name', read_only=True)
    plan_name = serializers.CharField(source='plan.name', read_only=True)

    class Meta:
        model = Commission
        fields = [
            'id', 'broker', 'broker_name', 'business', 'business_name',
            'subscription', 'plan', 'plan_name', 'period_start', 'period_end',
            'commission_amount', 'status', 'paid_at', 'payment_reference', 'notes',
            'created_at'
        ]
        read_only_fields = ['id', 'created_at']
