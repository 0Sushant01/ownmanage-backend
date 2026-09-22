from datetime import date
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.organization.models import Business, Branch
from apps.subscriptions.models import (
    Plan, Subscription, SubscriptionHistory,
    CentreCapacityAllocation, Commission, Broker, Referral,
    SubscriptionAction
)


def allocate_centre_capacity(subscription: Subscription, centre: Branch, new_capacity: int) -> CentreCapacityAllocation:
    """
    Allocates or updates employee capacity for a specific centre inside an atomic transaction.
    Enforces:
    1. Centre capacity cannot be reduced below its current active employee count.
    2. Sum of all centre capacities within the enterprise cannot exceed subscription plan capacity.
    """
    with transaction.atomic():
        sub = Subscription.objects.select_for_update().select_related('plan').get(id=subscription.id)
        current_active_employees = centre.employees.filter(employment_status='ACTIVE').count()

        if new_capacity < current_active_employees:
            raise ValidationError({
                'detail': f"Cannot set capacity for centre '{centre.name}' to {new_capacity}. "
                          f"Current active employee count is {current_active_employees}. "
                          f"Resolve active employees before reducing capacity."
            })

        allocations = list(CentreCapacityAllocation.objects.select_for_update().filter(subscription=sub))
        other_allocated = sum(a.allocated_capacity for a in allocations if a.centre_id != centre.id)
        new_total_allocated = other_allocated + new_capacity

        if new_total_allocated > sub.plan.total_employee_capacity:
            max_available = max(0, sub.plan.total_employee_capacity - other_allocated)
            raise ValidationError({
                'detail': f"Capacity allocation of {new_capacity} exceeds available plan capacity. "
                          f"Plan limit is {sub.plan.total_employee_capacity}, already allocated: {other_allocated}. "
                          f"Maximum available for this centre is {max_available}."
            })

        allocation, created = CentreCapacityAllocation.objects.get_or_create(
            subscription=sub,
            centre=centre,
            defaults={'allocated_capacity': new_capacity}
        )
        if not created:
            allocation.allocated_capacity = new_capacity
            allocation.save(update_fields=['allocated_capacity', 'updated_at'])

        return allocation


def change_subscription_plan(business: Business, new_plan: Plan, reason: str = "") -> Subscription:
    """
    Upgrades or downgrades an enterprise's subscription plan.
    Never destructively removes employees or centres.
    Records historical subscription snapshot for audit and billing provenance.
    """
    with transaction.atomic():
        biz = Business.objects.select_for_update().get(id=business.id)
        sub = Subscription.objects.select_for_update().filter(business=biz).first()

        current_centres_count = biz.branches.filter(is_active=True).count()
        if current_centres_count > new_plan.max_centres:
            raise ValidationError({
                'detail': f"Cannot change plan to '{new_plan.name}'. "
                          f"Enterprise currently has {current_centres_count} active centres, "
                          f"exceeding the plan maximum of {new_plan.max_centres}."
            })

        action = SubscriptionAction.INITIAL
        if sub:
            if new_plan.monthly_charge > sub.plan.monthly_charge:
                action = SubscriptionAction.UPGRADE
            elif new_plan.monthly_charge < sub.plan.monthly_charge:
                action = SubscriptionAction.DOWNGRADE
            else:
                action = SubscriptionAction.RENEWAL

        today = date.today()

        # Record historical snapshot
        SubscriptionHistory.objects.create(
            business=biz,
            plan=new_plan,
            action=action,
            monthly_charge=new_plan.monthly_charge,
            max_centres=new_plan.max_centres,
            total_employee_capacity=new_plan.total_employee_capacity,
            effective_from=today,
            reason=reason or f"Administrative plan change to {new_plan.name}"
        )

        if sub:
            sub.plan = new_plan
            sub.save(update_fields=['plan', 'updated_at'])
        else:
            sub = Subscription.objects.create(
                business=biz,
                plan=new_plan,
                start_date=today,
                current_period_start=today,
                current_period_end=today,
            )

        return sub


def generate_referral_commission(subscription: Subscription, period_start: date, period_end: date) -> Commission | None:
    """
    Generates immutable broker commission record for a billing period based on permanent referral.
    """
    referral = Referral.objects.filter(business=subscription.business).select_related('broker').first()
    if not referral or not referral.broker.is_active:
        return None

    commission_amount = (subscription.plan.monthly_charge * referral.broker.commission_rate) / 100

    commission, _ = Commission.objects.get_or_create(
        broker=referral.broker,
        business=subscription.business,
        period_start=period_start,
        period_end=period_end,
        defaults={
            'subscription': subscription,
            'plan': subscription.plan,
            'commission_amount': commission_amount,
            'status': 'PENDING',
            'notes': f"Calculated based on {referral.broker.commission_rate}% of {subscription.plan.monthly_charge}"
        }
    )
    return commission
