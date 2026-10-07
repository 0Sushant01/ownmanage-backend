from rest_framework import serializers
from apps.subscriptions.models import (
    Plan, Subscription, SubscriptionHistory,
    CentreCapacityAllocation, Broker, Referral, Commission
)


class PlanSerializer(serializers.ModelSerializer):
    active_subscribers_count = serializers.SerializerMethodField()
    total_subscribers_count = serializers.SerializerMethodField()
    used_employee_capacity = serializers.SerializerMethodField()
    available_employee_capacity = serializers.SerializerMethodField()

    class Meta:
        model = Plan
        fields = [
            'id', 'name', 'monthly_charge', 'max_centres',
            'total_employee_capacity', 'features', 'is_active',
            'active_subscribers_count', 'total_subscribers_count',
            'used_employee_capacity', 'available_employee_capacity',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_active_subscribers_count(self, obj) -> int:
        if hasattr(obj, 'annotated_active_subscribers'):
            return obj.annotated_active_subscribers
        return obj.subscriptions.filter(business__is_active=True).count()

    def get_total_subscribers_count(self, obj) -> int:
        if hasattr(obj, 'annotated_total_subscribers'):
            return obj.annotated_total_subscribers
        return obj.subscriptions.count()

    def get_used_employee_capacity(self, obj) -> int:
        if hasattr(obj, 'annotated_used_capacity'):
            return obj.annotated_used_capacity
        from apps.organization.models import Employee
        return Employee.objects.filter(
            business__subscription__plan=obj,
            employment_status='ACTIVE'
        ).count()

    def get_available_employee_capacity(self, obj) -> int:
        used = self.get_used_employee_capacity(obj)
        total_pool = obj.total_employee_capacity * max(1, self.get_active_subscribers_count(obj))
        return max(0, total_pool - used)


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
    days_remaining = serializers.IntegerField(read_only=True)
    payment_status = serializers.SerializerMethodField()
    last_payment = serializers.SerializerMethodField()
    next_payment_due = serializers.SerializerMethodField()
    broker = serializers.SerializerMethodField()

    class Meta:
        model = Subscription
        fields = [
            'id', 'business', 'plan', 'plan_name', 'monthly_charge',
            'max_centres', 'total_employee_capacity', 'features',
            'status', 'start_date', 'end_date', 'current_period_start', 'current_period_end',
            'days_remaining', 'payment_status', 'last_payment', 'next_payment_due', 'broker',
            'current_centres_count', 'current_employees_count',
            'total_allocated_capacity', 'unallocated_capacity',
            'centre_allocations', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'business', 'created_at', 'updated_at']

    def get_current_centres_count(self, obj) -> int:
        return obj.business.branches.filter(is_active=True).count()

    def get_current_employees_count(self, obj) -> int:
        return obj.business.employees.filter(employment_status='ACTIVE').count()

    def get_payment_status(self, obj) -> str:
        last_pmt = obj.payments.order_by('-billing_date').first()
        if last_pmt:
            return last_pmt.status
        return 'PAID' if obj.status in ['ACTIVE', 'ACTIVE_PAID'] else 'PENDING'

    def get_last_payment(self, obj):
        last_pmt = obj.payments.filter(status='PAID').order_by('-billing_date').first()
        if last_pmt:
            return {
                'amount': float(last_pmt.amount),
                'paid_at': last_pmt.paid_at.isoformat() if last_pmt.paid_at else str(last_pmt.billing_date),
                'invoice_number': last_pmt.invoice_number or last_pmt.payment_reference
            }
        return {
            'amount': float(obj.plan.monthly_charge),
            'paid_at': str(obj.current_period_start),
            'invoice_number': f"INV-{str(obj.id)[:8].upper()}"
        }

    def get_next_payment_due(self, obj) -> str:
        if obj.current_period_end:
            return str(obj.current_period_end)
        return str(obj.start_date)

    def get_broker(self, obj):
        ref = getattr(obj.business, 'referral', None)
        if ref and ref.broker:
            return {
                'id': str(ref.broker.id),
                'name': ref.broker.name,
                'referral_code': ref.broker.referral_code,
                'commission_rate': float(ref.broker.commission_rate)
            }
        return None

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        if instance.plan:
            ret['plan'] = PlanSerializer(instance.plan).data
            ret['plan_id'] = str(instance.plan.id)
            ret['plan_name'] = instance.plan.name
            ret['monthly_charge'] = str(instance.plan.monthly_charge)
            ret['max_centres'] = instance.plan.max_centres
            ret['total_employee_capacity'] = instance.plan.total_employee_capacity
        return ret


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
