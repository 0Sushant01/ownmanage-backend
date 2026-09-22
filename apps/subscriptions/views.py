from rest_framework import status, views
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, NotFound, ValidationError

from apps.core.permissions import get_user_context
from apps.organization.models import Business, Branch, BusinessRole
from apps.subscriptions.models import (
    Plan, Subscription, SubscriptionHistory,
    CentreCapacityAllocation, Broker, Referral, Commission
)
from apps.subscriptions.serializers import (
    PlanSerializer, SubscriptionSerializer, SubscriptionHistorySerializer,
    CentreCapacityAllocationSerializer, BrokerSerializer, ReferralSerializer,
    CommissionSerializer
)
from apps.subscriptions.services import allocate_centre_capacity, change_subscription_plan


class PlanListCreateView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Plan.objects.filter(is_active=True).order_by('monthly_charge')
        return Response(PlanSerializer(qs, many=True).data)

    def post(self, request):
        ctx = get_user_context(request)
        if not ctx['is_superadmin']:
            raise PermissionDenied('Only SuperAdmin can configure subscription plans.')

        serializer = PlanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        plan = serializer.save()
        return Response(PlanSerializer(plan).data, status=status.HTTP_201_CREATED)


class PlanDetailView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        plan = Plan.objects.filter(id=pk).first()
        if not plan:
            raise NotFound('Plan not found.')
        return Response(PlanSerializer(plan).data)

    def patch(self, request, pk):
        ctx = get_user_context(request)
        if not ctx['is_superadmin']:
            raise PermissionDenied('Only SuperAdmin can edit subscription plans.')
        plan = Plan.objects.filter(id=pk).first()
        if not plan:
            raise NotFound('Plan not found.')

        serializer = PlanSerializer(plan, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(PlanSerializer(plan).data)


class SubscriptionDetailView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        biz = ctx['business']
        if ctx['is_superadmin']:
            biz_id = request.query_params.get('business_id')
            if biz_id:
                biz = Business.objects.filter(id=biz_id).first()

        if not biz:
            raise ValidationError({'detail': 'No business context available.'})

        sub = Subscription.objects.filter(business=biz).select_related('plan').prefetch_related('centre_allocations').first()
        if not sub:
            return Response({'detail': 'No active subscription found for this enterprise.', 'has_subscription': False}, status=status.HTTP_404_NOT_FOUND)

        return Response(SubscriptionSerializer(sub).data)

    def post(self, request):
        """Assign or change subscription plan (Upgrade / Downgrade)"""
        ctx = get_user_context(request)
        if not ctx['is_superadmin']:
            raise PermissionDenied('Only SuperAdmin can assign or change enterprise subscription plans.')

        biz_id = request.data.get('business_id')
        plan_id = request.data.get('plan_id')
        reason = request.data.get('reason', '')

        if not biz_id or not plan_id:
            raise ValidationError({'detail': 'business_id and plan_id are required.'})

        biz = Business.objects.filter(id=biz_id).first()
        plan = Plan.objects.filter(id=plan_id).first()

        if not biz or not plan:
            raise NotFound('Business or Plan not found.')

        sub = change_subscription_plan(biz, plan, reason)
        return Response(SubscriptionSerializer(sub).data, status=status.HTTP_200_OK)


class CapacityReallocateView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        ctx = get_user_context(request)
        if not ctx['is_superadmin']:
            raise PermissionDenied('Only SuperAdmin can reallocate centre employee capacity.')

        centre_id = request.data.get('centre_id')
        new_capacity = request.data.get('allocated_capacity')

        if not centre_id or new_capacity is None:
            raise ValidationError({'detail': 'centre_id and allocated_capacity are required.'})

        centre = Branch.objects.filter(id=centre_id).select_related('business').first()
        if not centre:
            raise NotFound('Centre not found.')

        sub = Subscription.objects.filter(business=centre.business).first()
        if not sub:
            raise ValidationError({'detail': 'Enterprise has no active subscription.'})

        alloc = allocate_centre_capacity(sub, centre, int(new_capacity))
        return Response(CentreCapacityAllocationSerializer(alloc).data, status=status.HTTP_200_OK)


class BrokerListCreateView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        if not ctx['is_superadmin']:
            raise PermissionDenied('Only SuperAdmin can view all brokers.')

        qs = Broker.objects.all().select_related('user').prefetch_related('referrals', 'commissions')
        return Response(BrokerSerializer(qs, many=True).data)

    def post(self, request):
        ctx = get_user_context(request)
        if not ctx['is_superadmin']:
            raise PermissionDenied('Only SuperAdmin can create brokers.')

        email = request.data.get('email', '').strip().lower()
        name = request.data.get('name', '').strip()
        referral_code = request.data.get('referral_code', '').strip().upper()
        commission_rate = request.data.get('commission_rate', 10.00)
        password = request.data.get('password', 'Dev@123456')

        if not email or not name or not referral_code:
            raise ValidationError({'detail': 'email, name, and referral_code are required.'})

        from apps.accounts.models import User
        user, _ = User.objects.get_or_create(
            email=email,
            defaults={'first_name': name}
        )
        user.set_password(password)
        user.save()

        broker, created = Broker.objects.get_or_create(
            user=user,
            defaults={
                'name': name,
                'referral_code': referral_code,
                'commission_rate': commission_rate,
            }
        )
        if not created:
            broker.referral_code = referral_code
            broker.commission_rate = commission_rate
            broker.save()

        return Response(BrokerSerializer(broker).data, status=status.HTTP_201_CREATED)


class BrokerDashboardView(views.APIView):
    """
    Dedicated isolated dashboard for BROKER role.
    Strictly isolated: returns referral summaries, subscription status, and commissions.
    Zero access to enterprise operational data.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        broker = Broker.objects.filter(user=request.user).first()
        if not broker:
            # SuperAdmin can optionally view a broker dashboard via query param
            ctx = get_user_context(request)
            if ctx['is_superadmin']:
                broker_id = request.query_params.get('broker_id')
                if broker_id:
                    broker = Broker.objects.filter(id=broker_id).first()

        if not broker:
            raise PermissionDenied('You do not have an active Broker profile.')

        referrals = Referral.objects.filter(broker=broker).select_related('business')
        commissions = Commission.objects.filter(broker=broker).select_related('business', 'plan')

        # Compute high-level metrics
        total_businesses = referrals.count()
        active_businesses = 0
        trial_businesses = 0
        total_employees = 0
        plan_breakdown = {}

        for ref in referrals:
            sub = getattr(ref.business, 'subscription', None)
            if sub:
                if sub.status == 'ACTIVE':
                    active_businesses += 1
                elif sub.status == 'TRIAL':
                    trial_businesses += 1
                plan_name = sub.plan.name
                plan_breakdown[plan_name] = plan_breakdown.get(plan_name, 0) + 1
            total_employees += ref.business.employees.filter(employment_status='ACTIVE').count()

        total_earned = sum(c.commission_amount for c in commissions if c.status == 'PAID')
        pending_commission = sum(c.commission_amount for c in commissions if c.status == 'PENDING')
        approved_commission = sum(c.commission_amount for c in commissions if c.status == 'APPROVED')

        return Response({
            'broker': {
                'id': str(broker.id),
                'name': broker.name,
                'referral_code': broker.referral_code,
                'commission_rate': float(broker.commission_rate),
            },
            'metrics': {
                'total_referred_businesses': total_businesses,
                'active_businesses': active_businesses,
                'trial_businesses': trial_businesses,
                'total_referred_employees': total_employees,
                'plan_breakdown': plan_breakdown,
                'commission': {
                    'total_earned': float(total_earned),
                    'pending': float(pending_commission),
                    'approved': float(approved_commission),
                    'available': float(pending_commission + approved_commission),
                }
            },
            'referrals': ReferralSerializer(referrals, many=True).data,
            'recent_commissions': CommissionSerializer(commissions[:20], many=True).data,
        })


class CommissionListView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        if ctx['is_superadmin']:
            qs = Commission.objects.all().select_related('broker', 'business', 'plan').order_by('-created_at')
        elif hasattr(request.user, 'broker_profile'):
            qs = Commission.objects.filter(broker=request.user.broker_profile).select_related('broker', 'business', 'plan').order_by('-created_at')
        else:
            raise PermissionDenied('Access to commission records forbidden.')

        return Response(CommissionSerializer(qs[:100], many=True).data)
