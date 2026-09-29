from datetime import date, datetime, timedelta
from decimal import Decimal
from django.db.models import Sum, Count, Q, Prefetch
from django.utils import timezone
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied

from apps.core.permissions import get_user_context
from apps.organization.models import Business, Employee, EmploymentStatus
from apps.subscriptions.models import (
    Plan, Subscription, SubscriptionStatus, SubscriptionHistory,
    Broker, Referral, Commission, CommissionStatus,
    SubscriptionPayment, PaymentStatus
)


class SuperAdminAnalyticsView(APIView):
    """
    Authoritative platform-wide SaaS analytics endpoint for SuperAdmin.
    Calculates dynamic real-time metrics, growth rates, charts, and breakdowns
    based on filters: period, plan, business_status, subscription_status, broker.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        ctx = get_user_context(request)
        if not ctx['is_superadmin']:
            raise PermissionDenied("Only SuperAdmin can access platform analytics.")

        # 1. Parse Filter Parameters
        period = request.query_params.get('period', 'this_month')
        plan_id = request.query_params.get('plan_id', 'all')
        biz_status = request.query_params.get('business_status', 'all')
        sub_status = request.query_params.get('subscription_status', 'all')
        broker_id = request.query_params.get('broker_id', 'all')

        today = date.today()

        # 2. Determine Date Windows (Current Period & Previous Period for Growth %)
        start_date, end_date = self._get_date_range(period, request, today)
        duration_days = (end_date - start_date).days + 1
        prev_start = start_date - timedelta(days=duration_days)
        prev_end = start_date - timedelta(days=1)

        # 3. Base Querysets Scoped by Selected Filters
        businesses_qs = Business.objects.all()

        if biz_status == 'active':
            businesses_qs = businesses_qs.filter(is_active=True)
        elif biz_status == 'inactive':
            businesses_qs = businesses_qs.filter(is_active=False)

        if broker_id and broker_id != 'all':
            businesses_qs = businesses_qs.filter(referral__broker_id=broker_id)

        if plan_id and plan_id != 'all':
            businesses_qs = businesses_qs.filter(subscription__plan_id=plan_id)

        if sub_status and sub_status != 'all':
            if sub_status in ['PAID', 'ACTIVE_PAID']:
                businesses_qs = businesses_qs.filter(
                    subscription__status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.ACTIVE_PAID]
                )
            else:
                businesses_qs = businesses_qs.filter(subscription__status=sub_status)

        # 4. KPI Calculations (Optimized to bulk aggregations)
        biz_agg = businesses_qs.aggregate(
            total=Count('id'),
            new=Count('id', filter=Q(created_at__date__gte=start_date, created_at__date__lte=end_date)),
            prev_new=Count('id', filter=Q(created_at__date__gte=prev_start, created_at__date__lte=prev_end)),
            active=Count('id', filter=Q(is_active=True)),
        )
        total_biz = biz_agg['total']
        new_biz = biz_agg['new']
        prev_new_biz = biz_agg['prev_new']
        active_biz = biz_agg['active']
        inactive_biz = max(0, total_biz - active_biz)
        active_pct = round((active_biz / total_biz * 100) if total_biz > 0 else 0.0, 1)
        biz_growth_pct = self._calculate_growth(new_biz, prev_new_biz)

        emp_agg = Employee.objects.filter(business__in=businesses_qs).aggregate(
            total=Count('id'),
            active=Count('id', filter=Q(employment_status=EmploymentStatus.ACTIVE)),
            prev=Count('id', filter=Q(created_at__date__lte=prev_end)),
        )
        total_emp = emp_agg['total']
        active_emp = emp_agg['active']
        inactive_emp = max(0, total_emp - active_emp)
        emp_growth_pct = self._calculate_growth(total_emp, emp_agg['prev'])

        # Revenue aggregations for selected window
        payments_qs = SubscriptionPayment.objects.filter(business__in=businesses_qs)
        payments_agg = payments_qs.aggregate(
            current_paid=Sum('amount', filter=Q(status=PaymentStatus.PAID, billing_date__gte=start_date, billing_date__lte=end_date)),
            prev_paid=Sum('amount', filter=Q(status=PaymentStatus.PAID, billing_date__gte=prev_start, billing_date__lte=prev_end)),
            pending_amount=Sum('amount', filter=Q(status__in=[PaymentStatus.PENDING, PaymentStatus.OVERDUE], billing_date__gte=start_date, billing_date__lte=end_date)),
            pending_biz_count=Count('business', distinct=True, filter=Q(status__in=[PaymentStatus.PENDING, PaymentStatus.OVERDUE], billing_date__gte=start_date, billing_date__lte=end_date)),
        )
        current_paid_revenue = payments_agg['current_paid'] or Decimal('0.00')
        prev_paid_revenue = payments_agg['prev_paid'] or Decimal('0.00')
        revenue_growth_pct = self._calculate_growth(current_paid_revenue, prev_paid_revenue)
        payment_due_amount = payments_agg['pending_amount'] or Decimal('0.00')
        payment_due_biz_count = payments_agg['pending_biz_count']

        # Subscriptions KPI calculations
        sub_agg = Subscription.objects.filter(business__in=businesses_qs).aggregate(
            active=Count('id', filter=Q(status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.ACTIVE_PAID])),
            expiring_soon=Count('id', filter=Q(current_period_end__gte=today, current_period_end__lte=today + timedelta(days=7))),
            expired=Count('id', filter=Q(current_period_end__lt=today)),
        )
        active_subs = sub_agg['active']
        expiring_soon_subs = sub_agg['expiring_soon']
        expired_subs = sub_agg['expired']

        # Broker commission payable KPI
        comm_pending_qs = Commission.objects.filter(status=CommissionStatus.PENDING)
        comm_agg = comm_pending_qs.aggregate(
            amount=Sum('commission_amount'),
            brokers_count=Count('broker', distinct=True),
        )
        comm_pending_amount = comm_agg['amount'] or Decimal('0.00')
        comm_pending_brokers_count = comm_agg['brokers_count']

        # 5. Charts
        months_info = self._get_months_info(today)
        growth_chart = self._generate_growth_chart(businesses_qs, months_info)
        revenue_chart = self._generate_revenue_chart(businesses_qs, months_info)
        subscription_growth_chart = self._generate_subscription_growth_chart(businesses_qs, months_info, today)

        # 6. Status Breakdown & Plan Distribution
        status_breakdown = self._generate_status_breakdown(businesses_qs)
        plan_distribution = self._generate_plan_distribution(businesses_qs)
        broker_performance = self._generate_broker_performance()
        action_required = self._generate_action_required(today)
        recent_activity = self._generate_recent_activity(businesses_qs)
        expiring_subscriptions = self._generate_expiring_subscriptions(businesses_qs, today)

        payload = {
            'period': {
                'key': period,
                'start_date': str(start_date),
                'end_date': str(end_date),
            },
            'kpis': {
                'total_businesses': total_biz,
                'active_businesses': active_biz,
                'inactive_businesses': inactive_biz,
                'new_businesses': new_biz,
                'business_growth_pct': biz_growth_pct,
                'active_pct': active_pct,

                'active_subscriptions': active_subs,
                'paid_subscriptions': active_subs,
                'paid_pct': round((active_subs / total_biz * 100) if total_biz > 0 else 0.0, 1),
                'expiring_soon_subscriptions': expiring_soon_subs,
                'expired_subscriptions': expired_subs,

                'total_employees': total_emp,
                'active_employees': active_emp,
                'inactive_employees': inactive_emp,
                'employee_growth_pct': emp_growth_pct,

                'subscription_revenue': float(current_paid_revenue),
                'prev_month_revenue': float(prev_paid_revenue),
                'revenue_growth_pct': revenue_growth_pct,

                'payment_due_count': payment_due_biz_count,
                'payment_due_amount': float(payment_due_amount),
                'payment_due_businesses_count': payment_due_biz_count,

                'broker_commission_payable': float(comm_pending_amount),
                'broker_commission_payable_count': comm_pending_brokers_count,
            },
            'growth_chart': growth_chart,
            'revenue_chart': revenue_chart,
            'subscription_growth_chart': subscription_growth_chart,
            'status_breakdown': status_breakdown,
            'plan_distribution': plan_distribution,
            'broker_performance': broker_performance,
            'action_required': action_required,
            'recent_activity': recent_activity,
            'expiring_subscriptions': expiring_subscriptions,
        }

        return Response(payload, status=status.HTTP_200_OK)


    def _get_date_range(self, period: str, request, today: date):
        """Calculates start and end dates based on the selected period."""
        if period == 'today':
            return today, today
        elif period == 'this_week':
            start = today - timedelta(days=today.weekday())
            return start, today
        elif period == 'last_month':
            first_this_month = today.replace(day=1)
            end = first_this_month - timedelta(days=1)
            start = end.replace(day=1)
            return start, end
        elif period == 'last_3_months':
            return today - timedelta(days=90), today
        elif period == 'last_6_months':
            return today - timedelta(days=180), today
        elif period == 'this_year':
            return today.replace(month=1, day=1), today.replace(month=12, day=31)
        elif period == 'last_year':
            last_yr = today.year - 1
            return date(last_yr, 1, 1), date(last_yr, 12, 31)
        elif period == 'custom':
            try:
                start_str = request.query_params.get('start_date')
                end_str = request.query_params.get('end_date')
                if start_str and end_str:
                    return date.fromisoformat(start_str), date.fromisoformat(end_str)
            except Exception:
                pass
            return today.replace(day=1), today

        # Default: 'this_month'
        start = today.replace(day=1)
        # End of current month
        next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = next_month - timedelta(days=1)
        return start, end

    def _calculate_growth(self, current, previous) -> float:
        """Calculates percentage growth between two numbers."""
        c = float(current)
        p = float(previous)
        if p <= 0:
            return round(100.0 if c > 0 else 0.0, 1)
        return round(((c - p) / p) * 100.0, 1)

    def _get_months_info(self, today: date):
        """Precomputes 6 monthly calendar boundaries for all chart calculations."""
        months_info = []
        for i in range(5, -1, -1):
            year = today.year
            month = today.month - i
            while month <= 0:
                month += 12
                year -= 1
            m_start = date(year, month, 1)
            m_end = (m_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            label = m_start.strftime('%b %y')
            months_info.append({'label': label, 'start': m_start, 'end': m_end})
        return months_info

    def _generate_growth_chart(self, businesses_qs, months_info):
        """Generates 6 monthly data points for Business Growth using a single bulk query."""
        biz_data = list(businesses_qs.values('created_at__date', 'is_active', 'subscription__status'))
        points = []
        for m in months_info:
            month_start = m['start']
            month_end = m['end']
            total = sum(1 for b in biz_data if b['created_at__date'] <= month_end)
            new = sum(1 for b in biz_data if month_start <= b['created_at__date'] <= month_end)
            active = sum(1 for b in biz_data if b['created_at__date'] <= month_end and b['is_active'])
            churned = sum(1 for b in biz_data if b['created_at__date'] <= month_end and b['subscription__status'] in [SubscriptionStatus.CANCELLED, SubscriptionStatus.SUSPENDED])
            points.append({
                'month': m['label'],
                'total': total,
                'new': new,
                'active': active,
                'churned': churned,
            })
        return points

    def _generate_revenue_chart(self, businesses_qs, months_info):
        """Generates 6 monthly data points for Subscription Revenue using a single bulk query."""
        earliest_start = months_info[0]['start']
        latest_end = months_info[-1]['end']
        pmts = list(SubscriptionPayment.objects.filter(
            business__in=businesses_qs,
            billing_date__gte=earliest_start,
            billing_date__lte=latest_end
        ).values('billing_date', 'status', 'amount'))

        points = []
        for m in months_info:
            month_start = m['start']
            month_end = m['end']
            paid = sum((p['amount'] for p in pmts if month_start <= p['billing_date'] <= month_end and p['status'] == PaymentStatus.PAID), Decimal('0.00'))
            pending = sum((p['amount'] for p in pmts if month_start <= p['billing_date'] <= month_end and p['status'] in [PaymentStatus.PENDING, PaymentStatus.OVERDUE]), Decimal('0.00'))
            total = paid + pending
            points.append({
                'month': m['label'],
                'total_revenue': float(total),
                'paid_amount': float(paid),
                'pending_amount': float(pending),
            })
        return points

    def _generate_subscription_growth_chart(self, businesses_qs, months_info, today: date):
        """Generates 6 monthly data points for Subscription Growth using 2 bulk queries."""
        earliest_start = months_info[0]['start']
        latest_end = months_info[-1]['end']

        hist_data = list(SubscriptionHistory.objects.filter(
            business__in=businesses_qs,
            effective_from__gte=earliest_start,
            effective_from__lte=latest_end
        ).values('effective_from', 'action'))

        subs_data = list(Subscription.objects.filter(
            business__in=businesses_qs
        ).values('start_date', 'current_period_end'))

        points = []
        for m in months_info:
            month_start = m['start']
            month_end = m['end']

            new_subs = sum(1 for h in hist_data if month_start <= h['effective_from'] <= month_end and h['action'] == 'INITIAL')
            upgrades = sum(1 for h in hist_data if month_start <= h['effective_from'] <= month_end and h['action'] == 'UPGRADE')
            downgrades = sum(1 for h in hist_data if month_start <= h['effective_from'] <= month_end and h['action'] == 'DOWNGRADE')
            renewals = sum(1 for h in hist_data if month_start <= h['effective_from'] <= month_end and h['action'] == 'RENEWAL')
            cancelled = sum(1 for h in hist_data if month_start <= h['effective_from'] <= month_end and h['action'] == 'CANCEL')

            if new_subs == 0:
                new_subs = sum(1 for s in subs_data if s['start_date'] and month_start <= s['start_date'] <= month_end)

            expired = sum(1 for s in subs_data if s['current_period_end'] and month_start <= s['current_period_end'] <= month_end and s['current_period_end'] < today)

            points.append({
                'month': m['label'],
                'new': new_subs,
                'renewals': renewals,
                'upgrades': upgrades,
                'downgrades': downgrades,
                'expired': expired,
                'cancelled': cancelled,
                'total_activity': new_subs + renewals + upgrades + downgrades
            })
        return points

    def _generate_expiring_subscriptions(self, businesses_qs, today: date):
        """Builds the Expiring Subscriptions table data with prefetching to eliminate N+1."""
        subs = Subscription.objects.filter(
            business__in=businesses_qs,
            current_period_end__isnull=False
        ).select_related('business', 'plan').prefetch_related(
            'business__referral__broker',
            Prefetch('payments', queryset=SubscriptionPayment.objects.order_by('-billing_date'), to_attr='prefetched_payments')
        ).order_by('current_period_end')

        results = []
        for s in subs:
            days = (s.current_period_end - today).days
            if days <= 30:
                if days <= 3:
                    urgency = 'critical'
                elif days <= 7:
                    urgency = 'warning'
                else:
                    urgency = 'upcoming'

                ref = getattr(s.business, 'referral', None)
                payments = getattr(s, 'prefetched_payments', None)
                last_pmt = payments[0] if payments else None
                payment_status = last_pmt.status if last_pmt else ('PAID' if s.status in ['ACTIVE', 'ACTIVE_PAID'] else 'PENDING')

                results.append({
                    'id': str(s.id),
                    'business_id': str(s.business.id),
                    'business_name': s.business.name,
                    'current_plan': s.plan.name,
                    'monthly_charge': float(s.plan.monthly_charge),
                    'expiry_date': str(s.current_period_end),
                    'days_remaining': days,
                    'urgency': urgency,
                    'payment_status': payment_status,
                    'assigned_broker': ref.broker.name if (ref and ref.broker) else 'Direct / Organic',
                })
        return results

    def _generate_status_breakdown(self, businesses_qs):
        """Categorizes subscriptions by status and computes revenue totals using grouped aggregation."""
        statuses = [
            ('ACTIVE_PAID', 'Active Paid', ['ACTIVE', 'ACTIVE_PAID']),
            ('TRIAL', 'Trial', ['TRIAL']),
            ('PAYMENT_DUE', 'Payment Due', ['PAYMENT_DUE']),
            ('OVERDUE', 'Overdue', ['OVERDUE']),
            ('EXPIRED', 'Expired', ['EXPIRED']),
            ('SUSPENDED', 'Suspended', ['SUSPENDED']),
            ('CANCELLED', 'Cancelled', ['CANCELLED']),
        ]

        total_biz = businesses_qs.count()
        status_counts_raw = dict(
            businesses_qs.values('subscription__status').annotate(c=Count('id')).values_list('subscription__status', 'c')
        )
        rev_agg = SubscriptionPayment.objects.filter(business__in=businesses_qs).aggregate(
            paid=Sum('amount', filter=Q(status=PaymentStatus.PAID)),
            pending=Sum('amount', filter=Q(status__in=[PaymentStatus.PENDING, PaymentStatus.OVERDUE]))
        )
        paid_rev = rev_agg['paid'] or Decimal('0.00')
        pending_rev = rev_agg['pending'] or Decimal('0.00')

        items = []
        for key, label, match_list in statuses:
            count = sum(status_counts_raw.get(s, 0) for s in match_list)
            pct = round((count / total_biz * 100) if total_biz > 0 else 0.0, 1)
            items.append({
                'key': key,
                'label': label,
                'count': count,
                'percentage': pct,
            })

        return {
            'statuses': items,
            'paid_revenue': float(paid_rev),
            'pending_revenue': float(pending_rev),
        }

    def _generate_plan_distribution(self, businesses_qs):
        """Computes business distribution and revenue per commercial plan using bulk annotations."""
        plans = list(Plan.objects.all().order_by('monthly_charge'))
        biz_plan_stats = {
            row['subscription__plan']: row
            for row in businesses_qs.values('subscription__plan').annotate(
                biz_count=Count('id'),
                active_count=Count('id', filter=Q(is_active=True)),
                paid_count=Count('id', filter=Q(subscription__status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.ACTIVE_PAID]))
            )
        }
        rev_plan_stats = {
            row['plan']: row['rev']
            for row in SubscriptionPayment.objects.filter(business__in=businesses_qs, status=PaymentStatus.PAID).values('plan').annotate(rev=Sum('amount'))
        }

        distribution = []
        for p in plans:
            p_stat = biz_plan_stats.get(p.id, {})
            biz_count = p_stat.get('biz_count', 0)
            active_count = p_stat.get('active_count', 0)
            paid_count = p_stat.get('paid_count', 0)
            rev = rev_plan_stats.get(p.id, Decimal('0.00')) or Decimal('0.00')

            distribution.append({
                'id': str(p.id),
                'name': p.name,
                'monthly_charge': float(p.monthly_charge),
                'max_centres': p.max_centres,
                'total_capacity': p.total_employee_capacity,
                'businesses_count': biz_count,
                'active_count': active_count,
                'paid_count': paid_count,
                'revenue': float(rev),
            })
        return distribution

    def _generate_broker_performance(self):
        """Summarizes client acquisition and commissions for brokers using bulk prefetched data."""
        brokers = list(Broker.objects.all())
        refs = list(Referral.objects.select_related('business').all())
        comms = list(Commission.objects.all())
        pmts_rev = dict(
            SubscriptionPayment.objects.filter(status=PaymentStatus.PAID).values('business_id').annotate(s=Sum('amount')).values_list('business_id', 's')
        )

        performance = []
        for b in brokers:
            b_refs = [r for r in refs if r.broker_id == b.id]
            b_comms = [c for c in comms if c.broker_id == b.id]

            referred_count = len(b_refs)
            active_count = sum(1 for r in b_refs if r.business.is_active)
            total_rev = sum((pmts_rev.get(r.business_id, Decimal('0.00')) for r in b_refs), Decimal('0.00'))

            comm_paid = sum((c.commission_amount for c in b_comms if c.status == CommissionStatus.PAID), Decimal('0.00'))
            comm_pending = sum((c.commission_amount for c in b_comms if c.status == CommissionStatus.PENDING), Decimal('0.00'))

            performance.append({
                'id': str(b.id),
                'name': b.name,
                'referral_code': b.referral_code,
                'commission_rate': float(b.commission_rate),
                'referred_businesses': referred_count,
                'active_businesses': active_count,
                'total_revenue': float(total_rev),
                'commissions_paid': float(comm_paid),
                'commissions_pending': float(comm_pending),
            })
        return performance

    def _generate_action_required(self, today: date):
        """Computes operational items needing SuperAdmin intervention using combined aggregations."""
        overdue_agg = SubscriptionPayment.objects.filter(status=PaymentStatus.OVERDUE).aggregate(
            c=Count('id'), s=Sum('amount')
        )
        overdue_count = overdue_agg['c']
        overdue_amount = overdue_agg['s'] or Decimal('0.00')

        expiring_soon_count = Subscription.objects.filter(
            current_period_end__gte=today,
            current_period_end__lte=today + timedelta(days=7)
        ).count()

        suspended_count = Business.objects.filter(
            Q(is_active=False) | Q(subscription__status=SubscriptionStatus.SUSPENDED)
        ).distinct().count()

        comm_agg = Commission.objects.filter(status=CommissionStatus.PENDING).aggregate(
            c=Count('id'), s=Sum('commission_amount')
        )
        commissions_pending_count = comm_agg['c']
        commissions_pending_amount = comm_agg['s'] or Decimal('0.00')

        trials_count = Subscription.objects.filter(status=SubscriptionStatus.TRIAL).count()

        return {
            'payments_overdue_count': overdue_count,
            'payments_overdue_amount': float(overdue_amount),
            'subscriptions_expiring_soon': expiring_soon_count,
            'businesses_suspended': suspended_count,
            'commissions_pending_count': commissions_pending_count,
            'commissions_pending_amount': float(commissions_pending_amount),
            'trials_count': trials_count,
        }

    def _generate_recent_activity(self, businesses_qs):
        """Assembles a unified chronological stream of latest platform activity."""
        activities = []

        # 1. New Business Signups (last 5)
        recent_biz = businesses_qs.order_by('-created_at')[:5]
        for b in recent_biz:
            activities.append({
                'id': f"biz_{b.id}",
                'type': 'BUSINESS_REGISTERED',
                'title': 'New business registered',
                'description': b.name,
                'timestamp': b.created_at.isoformat(),
                'status_color': 'emerald',
            })

        # 2. Recent Payments (last 5)
        recent_pmts = SubscriptionPayment.objects.filter(
            business__in=businesses_qs
        ).select_related('business', 'plan').order_by('-created_at')[:5]
        for p in recent_pmts:
            activities.append({
                'id': f"pmt_{p.id}",
                'type': 'PAYMENT_RECEIVED' if p.status == PaymentStatus.PAID else 'PAYMENT_DUE',
                'title': 'Payment received' if p.status == PaymentStatus.PAID else 'Payment invoice issued',
                'description': f"{p.business.name} — ₹{p.amount:,.0f} ({p.plan.name})",
                'timestamp': p.created_at.isoformat(),
                'status_color': 'blue' if p.status == PaymentStatus.PAID else 'amber',
            })

        # 3. Recent Commission Payouts (last 3)
        recent_comms = Commission.objects.select_related('broker', 'business').order_by('-created_at')[:3]
        for c in recent_comms:
            activities.append({
                'id': f"comm_{c.id}",
                'type': 'COMMISSION_UPDATE',
                'title': f"Commission {c.status.lower()}",
                'description': f"{c.broker.name} — ₹{c.commission_amount:,.0f} ({c.business.name})",
                'timestamp': c.created_at.isoformat(),
                'status_color': 'purple',
            })

        # Sort combined activity chronologically descending and take top 10
        activities.sort(key=lambda x: x['timestamp'], reverse=True)
        return activities[:10]
