from datetime import date, datetime, timedelta
from decimal import Decimal
from django.db.models import Sum, Count, Q
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

        # 4. KPI Calculations
        total_biz = businesses_qs.count()
        new_biz = businesses_qs.filter(created_at__date__gte=start_date, created_at__date__lte=end_date).count()
        prev_new_biz = businesses_qs.filter(created_at__date__gte=prev_start, created_at__date__lte=prev_end).count()
        biz_growth_pct = self._calculate_growth(new_biz, prev_new_biz)

        active_biz = businesses_qs.filter(is_active=True).count()
        active_pct = round((active_biz / total_biz * 100) if total_biz > 0 else 0.0, 1)

        total_emp = Employee.objects.filter(business__in=businesses_qs).count()
        prev_emp = Employee.objects.filter(
            business__in=businesses_qs,
            created_at__date__lte=prev_end
        ).count()
        emp_growth_pct = self._calculate_growth(total_emp, prev_emp)

        # Revenue aggregations for selected window
        payments_qs = SubscriptionPayment.objects.filter(business__in=businesses_qs)

        current_paid_revenue = payments_qs.filter(
            status=PaymentStatus.PAID,
            billing_date__gte=start_date,
            billing_date__lte=end_date
        ).aggregate(sum=Sum('amount'))['sum'] or Decimal('0.00')

        prev_paid_revenue = payments_qs.filter(
            status=PaymentStatus.PAID,
            billing_date__gte=prev_start,
            billing_date__lte=prev_end
        ).aggregate(sum=Sum('amount'))['sum'] or Decimal('0.00')

        revenue_growth_pct = self._calculate_growth(current_paid_revenue, prev_paid_revenue)

        # Subscriptions payment status
        paid_subs = Subscription.objects.filter(
            business__in=businesses_qs,
            status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.ACTIVE_PAID]
        ).count()
        paid_subs_pct = round((paid_subs / total_biz * 100) if total_biz > 0 else 0.0, 1)

        pending_payments = payments_qs.filter(
            status__in=[PaymentStatus.PENDING, PaymentStatus.OVERDUE],
            billing_date__gte=start_date,
            billing_date__lte=end_date
        )
        payment_due_count = pending_payments.count()
        payment_due_amount = pending_payments.aggregate(sum=Sum('amount'))['sum'] or Decimal('0.00')

        # 5. Monthly Business Growth Chart Data (Last 6 Months Time Series)
        growth_chart = self._generate_growth_chart(businesses_qs, today)

        # 6. Monthly Subscription Revenue Trend Chart Data (Last 6 Months Time Series)
        revenue_chart = self._generate_revenue_chart(businesses_qs, today)

        # 7. Subscription Status Breakdown
        status_breakdown = self._generate_status_breakdown(businesses_qs)

        # 8. Plan Distribution Table
        plan_distribution = self._generate_plan_distribution(businesses_qs)

        # 9. Broker Performance Summary
        broker_performance = self._generate_broker_performance()

        # 10. Action Required Alerts
        action_required = self._generate_action_required(today)

        # 11. Recent Platform Activity
        recent_activity = self._generate_recent_activity(businesses_qs)

        payload = {
            'period': {
                'key': period,
                'start_date': str(start_date),
                'end_date': str(end_date),
            },
            'kpis': {
                'total_businesses': total_biz,
                'new_businesses': new_biz,
                'business_growth_pct': biz_growth_pct,
                'active_businesses': active_biz,
                'active_pct': active_pct,
                'total_employees': total_emp,
                'employee_growth_pct': emp_growth_pct,
                'subscription_revenue': float(current_paid_revenue),
                'revenue_growth_pct': revenue_growth_pct,
                'paid_subscriptions': paid_subs,
                'paid_pct': paid_subs_pct,
                'payment_due_count': payment_due_count,
                'payment_due_amount': float(payment_due_amount),
            },
            'growth_chart': growth_chart,
            'revenue_chart': revenue_chart,
            'status_breakdown': status_breakdown,
            'plan_distribution': plan_distribution,
            'broker_performance': broker_performance,
            'action_required': action_required,
            'recent_activity': recent_activity,
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

    def _generate_growth_chart(self, businesses_qs, today: date):
        """Generates 6 monthly data points for the Business Growth line/area chart."""
        points = []
        for i in range(5, -1, -1):
            # Compute month anchor
            year = today.year
            month = today.month - i
            while month <= 0:
                month += 12
                year -= 1

            month_start = date(year, month, 1)
            month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            label = month_start.strftime('%b %y')

            total = businesses_qs.filter(created_at__date__lte=month_end).count()
            new = businesses_qs.filter(
                created_at__date__gte=month_start,
                created_at__date__lte=month_end
            ).count()
            active = businesses_qs.filter(
                created_at__date__lte=month_end,
                is_active=True
            ).count()
            churned = businesses_qs.filter(
                created_at__date__lte=month_end,
                subscription__status__in=[SubscriptionStatus.CANCELLED, SubscriptionStatus.SUSPENDED]
            ).count()

            points.append({
                'month': label,
                'total': total,
                'new': new,
                'active': active,
                'churned': churned,
            })
        return points

    def _generate_revenue_chart(self, businesses_qs, today: date):
        """Generates 6 monthly data points for the Subscription Revenue trend chart."""
        points = []
        for i in range(5, -1, -1):
            year = today.year
            month = today.month - i
            while month <= 0:
                month += 12
                year -= 1

            month_start = date(year, month, 1)
            month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            label = month_start.strftime('%b %y')

            pmts = SubscriptionPayment.objects.filter(
                business__in=businesses_qs,
                billing_date__gte=month_start,
                billing_date__lte=month_end
            )

            paid = pmts.filter(status=PaymentStatus.PAID).aggregate(sum=Sum('amount'))['sum'] or Decimal('0.00')
            pending = pmts.filter(status__in=[PaymentStatus.PENDING, PaymentStatus.OVERDUE]).aggregate(sum=Sum('amount'))['sum'] or Decimal('0.00')
            total = paid + pending

            points.append({
                'month': label,
                'total_revenue': float(total),
                'paid_amount': float(paid),
                'pending_amount': float(pending),
            })
        return points

    def _generate_status_breakdown(self, businesses_qs):
        """Categorizes subscriptions by status and computes revenue totals."""
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
        items = []
        total_paid_revenue = Decimal('0.00')
        total_pending_revenue = Decimal('0.00')

        for key, label, match_list in statuses:
            count = businesses_qs.filter(subscription__status__in=match_list).count()
            pct = round((count / total_biz * 100) if total_biz > 0 else 0.0, 1)
            items.append({
                'key': key,
                'label': label,
                'count': count,
                'percentage': pct,
            })

        # Calculate revenue by status
        paid_rev = SubscriptionPayment.objects.filter(
            business__in=businesses_qs,
            status=PaymentStatus.PAID
        ).aggregate(sum=Sum('amount'))['sum'] or Decimal('0.00')

        pending_rev = SubscriptionPayment.objects.filter(
            business__in=businesses_qs,
            status__in=[PaymentStatus.PENDING, PaymentStatus.OVERDUE]
        ).aggregate(sum=Sum('amount'))['sum'] or Decimal('0.00')

        return {
            'statuses': items,
            'paid_revenue': float(paid_rev),
            'pending_revenue': float(pending_rev),
        }

    def _generate_plan_distribution(self, businesses_qs):
        """Computes business distribution and revenue per commercial plan."""
        plans = Plan.objects.all().order_by('monthly_charge')
        distribution = []

        for p in plans:
            biz_count = businesses_qs.filter(subscription__plan=p).count()
            active_count = businesses_qs.filter(
                subscription__plan=p,
                is_active=True
            ).count()
            paid_count = businesses_qs.filter(
                subscription__plan=p,
                subscription__status__in=[SubscriptionStatus.ACTIVE, SubscriptionStatus.ACTIVE_PAID]
            ).count()

            rev = SubscriptionPayment.objects.filter(
                business__in=businesses_qs,
                plan=p,
                status=PaymentStatus.PAID
            ).aggregate(sum=Sum('amount'))['sum'] or Decimal('0.00')

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
        """Summarizes client acquisition and commissions for top brokers."""
        brokers = Broker.objects.all().prefetch_related('referrals', 'commissions')
        performance = []

        for b in brokers:
            referred_count = b.referrals.count()
            active_count = b.referrals.filter(business__is_active=True).count()

            biz_ids = b.referrals.values_list('business_id', flat=True)
            rev = SubscriptionPayment.objects.filter(
                business_id__in=biz_ids,
                status=PaymentStatus.PAID
            ).aggregate(sum=Sum('amount'))['sum'] or Decimal('0.00')

            comm_paid = b.commissions.filter(
                status=CommissionStatus.PAID
            ).aggregate(sum=Sum('commission_amount'))['sum'] or Decimal('0.00')

            comm_pending = b.commissions.filter(
                status=CommissionStatus.PENDING
            ).aggregate(sum=Sum('commission_amount'))['sum'] or Decimal('0.00')

            performance.append({
                'id': str(b.id),
                'name': b.name,
                'referral_code': b.referral_code,
                'commission_rate': float(b.commission_rate),
                'referred_businesses': referred_count,
                'active_businesses': active_count,
                'total_revenue': float(rev),
                'commissions_paid': float(comm_paid),
                'commissions_pending': float(comm_pending),
            })
        return performance

    def _generate_action_required(self, today: date):
        """Computes operational items needing SuperAdmin intervention."""
        overdue_payments = SubscriptionPayment.objects.filter(status=PaymentStatus.OVERDUE)
        overdue_count = overdue_payments.count()
        overdue_amount = overdue_payments.aggregate(sum=Sum('amount'))['sum'] or Decimal('0.00')

        expiring_soon_count = Subscription.objects.filter(
            current_period_end__gte=today,
            current_period_end__lte=today + timedelta(days=7)
        ).count()

        suspended_count = Business.objects.filter(
            Q(is_active=False) | Q(subscription__status=SubscriptionStatus.SUSPENDED)
        ).distinct().count()

        commissions_pending_qs = Commission.objects.filter(status=CommissionStatus.PENDING)
        commissions_pending_count = commissions_pending_qs.count()
        commissions_pending_amount = commissions_pending_qs.aggregate(sum=Sum('commission_amount'))['sum'] or Decimal('0.00')

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
