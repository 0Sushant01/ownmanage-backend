from rest_framework import status, views
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied, NotFound, ValidationError

from django.db.models import Count, Q
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


def get_annotated_plans_queryset():
    return Plan.objects.annotate(
        annotated_total_subscribers=Count('subscriptions', distinct=True),
        annotated_active_subscribers=Count('subscriptions', filter=Q(subscriptions__business__is_active=True), distinct=True),
        annotated_used_capacity=Count(
            'subscriptions__business__employees',
            filter=Q(
                subscriptions__business__employees__employment_status='ACTIVE',
                subscriptions__business__is_active=True
            ),
            distinct=True
        )
    )


class PlanListCreateView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = get_annotated_plans_queryset().filter(is_active=True).order_by('monthly_charge')
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
        plan = get_annotated_plans_queryset().filter(id=pk).first()
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

        new_capacity = request.data.get('total_employee_capacity')
        if new_capacity is not None:
            new_cap_int = int(new_capacity)
            for sub in plan.subscriptions.filter(business__is_active=True).select_related('business'):
                active_count = sub.business.employees.filter(employment_status='ACTIVE').count()
                if active_count > new_cap_int:
                    raise ValidationError({
                        'detail': f"Current usage: {active_count} / {plan.total_employee_capacity} seats in '{sub.business.name}'. "
                                  f"New capacity cannot be lower than current active usage."
                    })

        serializer = PlanSerializer(plan, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated_plan = serializer.save()

        from apps.core.audit import record_audit_log
        record_audit_log(
            action='PLAN_UPDATED',
            entity_type='Plan',
            entity_id=str(plan.id),
            actor=request.user,
            new_data={'name': updated_plan.name, 'monthly_charge': float(updated_plan.monthly_charge)},
            request=request
        )
        return Response(PlanSerializer(updated_plan).data)

    def delete(self, request, pk):
        ctx = get_user_context(request)
        if not ctx['is_superadmin']:
            raise PermissionDenied('Only SuperAdmin can deactivate subscription plans.')
        plan = Plan.objects.filter(id=pk).first()
        if not plan:
            raise NotFound('Plan not found.')

        has_subscribers = plan.subscriptions.exists()
        if has_subscribers:
            plan.is_active = False
            plan.save(update_fields=['is_active', 'updated_at'])
            msg = 'Plan has active/historical subscribers and has been safely deactivated.'
        else:
            plan.delete()
            msg = 'Plan has zero subscribers and has been permanently deleted.'

        from apps.core.audit import record_audit_log
        record_audit_log(
            action='PLAN_DEACTIVATED' if has_subscribers else 'PLAN_DELETED',
            entity_type='Plan',
            entity_id=str(pk),
            actor=request.user,
            request=request
        )
        return Response({'detail': msg})


class PlanSubscribersView(views.APIView):
    """
    Returns the list of businesses currently subscribing to a commercial plan.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        ctx = get_user_context(request)
        if not ctx['is_superadmin']:
            raise PermissionDenied('Only SuperAdmin can view plan subscribers.')
        plan = Plan.objects.filter(id=pk).first()
        if not plan:
            raise NotFound('Plan not found.')

        subs = Subscription.objects.filter(plan=plan).select_related('business').prefetch_related('business__referral__broker')
        subscribers = []
        for s in subs:
            b = s.business
            ref = getattr(b, 'referral', None)
            last_pmt = s.payments.order_by('-billing_date').first()
            subscribers.append({
                'id': str(s.id),
                'business_id': str(b.id),
                'business_name': b.name,
                'business_status': 'Active' if b.is_active else 'Inactive',
                'subscription_status': s.status,
                'start_date': str(s.start_date),
                'expiry_date': str(s.current_period_end),
                'days_remaining': s.days_remaining,
                'payment_status': last_pmt.status if last_pmt else ('PAID' if s.status in ['ACTIVE', 'ACTIVE_PAID'] else 'PENDING'),
                'active_employees': b.employees.filter(employment_status='ACTIVE').count(),
                'total_capacity': plan.total_employee_capacity,
                'centres_count': b.branches.filter(is_active=True).count(),
                'broker_name': ref.broker.name if (ref and ref.broker) else 'Direct',
            })
        return Response({
            'plan': PlanSerializer(plan).data,
            'plan_id': str(plan.id),
            'plan_name': plan.name,
            'total_subscribers': len(subscribers),
            'subscribers': subscribers
        })



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


class BrokerDetailView(views.APIView):
    """
    Detailed broker profile and referred client portfolio for SuperAdmin management.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        ctx = get_user_context(request)
        if not ctx['is_superadmin']:
            raise PermissionDenied('Only SuperAdmin can view full broker details.')
        broker = Broker.objects.filter(id=pk).select_related('user').first()
        if not broker:
            raise NotFound('Broker not found.')

        referrals = Referral.objects.filter(broker=broker).select_related('business').prefetch_related('business__subscription__plan')
        commissions = Commission.objects.filter(broker=broker).select_related('business', 'plan').order_by('-period_start')

        referred_data = []
        for r in referrals:
            b = r.business
            sub = getattr(b, 'subscription', None)
            latest_comm = commissions.filter(business=b).first()
            last_pmt = sub.payments.order_by('-billing_date').first() if sub else None
            referred_data.append({
                'id': str(r.id),
                'business_id': str(b.id),
                'business_name': b.name,
                'plan_name': sub.plan.name if sub else 'None',
                'subscription_amount': float(sub.plan.monthly_charge) if sub else 0.0,
                'subscription_status': sub.status if sub else 'INACTIVE',
                'payment_status': last_pmt.status if last_pmt else ('PAID' if (sub and sub.status in ['ACTIVE', 'ACTIVE_PAID']) else 'PENDING'),
                'commission_rate': float(broker.commission_rate),
                'commission_amount': float(latest_comm.commission_amount) if latest_comm else float((sub.plan.monthly_charge * broker.commission_rate) / 100 if sub else 0),
                'commission_status': latest_comm.status if latest_comm else 'PENDING',
                'subscription_date': str(sub.start_date) if sub else str(r.referred_at.date()),
                'expiry_date': str(sub.current_period_end) if sub else '—',
            })

        total_earned = sum(c.commission_amount for c in commissions if c.status == 'PAID')
        pending = sum(c.commission_amount for c in commissions if c.status == 'PENDING')

        return Response({
            'broker': BrokerSerializer(broker).data,
            'kpis': {
                'total_referrals': referrals.count(),
                'active_businesses': referrals.filter(business__is_active=True).count(),
                'total_revenue_generated': float(sum(r['subscription_amount'] for r in referred_data)),
                'commission_earned': float(total_earned + pending),
                'commission_paid': float(total_earned),
                'commission_pending': float(pending),
            },
            'referred_businesses': referred_data,
            'commissions': CommissionSerializer(commissions[:50], many=True).data,
        })

    def patch(self, request, pk):
        ctx = get_user_context(request)
        if not ctx['is_superadmin']:
            raise PermissionDenied('Only SuperAdmin can edit broker details.')
        broker = Broker.objects.filter(id=pk).first()
        if not broker:
            raise NotFound('Broker not found.')

        old_rate = float(broker.commission_rate)
        new_rate = request.data.get('commission_rate')

        serializer = BrokerSerializer(broker, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

        if new_rate is not None and float(new_rate) != old_rate:
            from apps.core.audit import record_audit_log
            record_audit_log(
                action='BROKER_COMMISSION_RATE_CHANGED',
                entity_type='Broker',
                entity_id=str(broker.id),
                actor=request.user,
                old_data={'commission_rate': old_rate},
                new_data={'commission_rate': float(new_rate)},
                request=request
            )

        return Response(BrokerSerializer(broker).data)


class PayCommissionView(views.APIView):
    """
    SuperAdmin action to mark a broker commission as paid.
    Requires transaction reference and logs immutable audit trail.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        ctx = get_user_context(request)
        if not ctx['is_superadmin']:
            raise PermissionDenied('Only SuperAdmin can record commission payouts.')

        comm = Commission.objects.filter(id=pk).first()
        if not comm:
            raise NotFound('Commission record not found.')

        payment_reference = request.data.get('payment_reference', '').strip()
        notes = request.data.get('notes', '').strip()

        if not payment_reference:
            raise ValidationError({'detail': 'Payment reference / transaction ID is required.'})

        from django.utils import timezone
        comm.status = 'PAID'
        comm.paid_at = timezone.now()
        comm.payment_reference = payment_reference
        if notes:
            comm.notes = f"{comm.notes}\n{notes}".strip()
        comm.save(update_fields=['status', 'paid_at', 'payment_reference', 'notes', 'updated_at'])

        from apps.core.audit import record_audit_log
        record_audit_log(
            action='BROKER_COMMISSION_PAID',
            entity_type='Commission',
            entity_id=str(comm.id),
            actor=request.user,
            new_data={
                'broker_id': str(comm.broker.id),
                'amount': float(comm.commission_amount),
                'payment_reference': payment_reference,
            },
            request=request
        )

        return Response(CommissionSerializer(comm).data, status=status.HTTP_200_OK)


class BusinessSubscriptionHistoryView(views.APIView):
    """
    Retrieves full audit history of plan upgrades, downgrades, and renewals for an enterprise.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        biz = Business.objects.filter(id=pk).first()
        if not biz:
            raise NotFound('Business not found.')

        history = SubscriptionHistory.objects.filter(business=biz).order_by('-created_at')
        return Response(SubscriptionHistorySerializer(history, many=True).data)

