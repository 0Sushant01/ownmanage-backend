import calendar
from datetime import date, timedelta
from typing import Dict, Any, List, Optional, Tuple
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from apps.core.services.audit_service import AuditService
from apps.organization.models import Business, Branch, Employee
from apps.payroll.models import (
    PayrollScheduleConfig, PayrollScheduleHistory, ScheduleConfigScope,
    CompensationType, PayFrequency, MonthEndRule,
    PayrollGenerationMode, PaymentScheduleRule,
    PayrollRun, PayrollRunStatus, PayrollStatus
)


SYSTEM_DEFAULT_SCHEDULE: Dict[str, Any] = {
    'compensation_type': CompensationType.MONTHLY_SALARY,
    'pay_frequency': PayFrequency.MONTHLY_CALENDAR,
    'week_start_day': 0,  # Monday
    'custom_cycle_start_day': 1,
    'anchor_date': None,
    'month_end_rule': MonthEndRule.CLAMP_TO_LAST_DAY,
    'generation_mode': PayrollGenerationMode.MANUAL,
    'generation_delay_days': 1,
    'generation_day_of_month': 1,
    'approval_required': True,
    'approver_role': 'BUSINESS_ADMIN',
    'review_deadline_days': 3,
    'payment_rule': PaymentScheduleRule.DAY_OF_FOLLOWING_MONTH,
    'payment_offset_days': 5,
    'payment_day_of_month': 7,
    'payment_weekday': 4,  # Friday
}


class PayrollScheduleService:
    CONFIG_FIELDS = [
        'compensation_type', 'pay_frequency', 'week_start_day',
        'custom_cycle_start_day', 'anchor_date', 'month_end_rule',
        'generation_mode', 'generation_delay_days', 'generation_day_of_month',
        'approval_required', 'approver_role', 'review_deadline_days',
        'payment_rule', 'payment_offset_days', 'payment_day_of_month', 'payment_weekday',
        'effective_from', 'effective_to', 'change_reason'
    ]

    @classmethod
    def serialize_config_dict(cls, obj: Optional[PayrollScheduleConfig]) -> Optional[Dict[str, Any]]:
        if not obj:
            return None
        return {
            'id': str(obj.id),
            'scope': obj.scope,
            'has_override': obj.has_override,
            'is_active': obj.is_active,
            'effective_from': str(obj.effective_from) if obj.effective_from else None,
            'effective_to': str(obj.effective_to) if obj.effective_to else None,
            'change_reason': obj.change_reason or '',
            'changed_by_name': obj.changed_by.get_full_name() or obj.changed_by.email if obj.changed_by else None,
            'updated_at': obj.updated_at.isoformat() if obj.updated_at else None,
            'compensation_type': obj.compensation_type,
            'pay_frequency': obj.pay_frequency,
            'week_start_day': obj.week_start_day,
            'custom_cycle_start_day': obj.custom_cycle_start_day,
            'anchor_date': str(obj.anchor_date) if obj.anchor_date else None,
            'month_end_rule': obj.month_end_rule,
            'generation_mode': obj.generation_mode,
            'generation_delay_days': obj.generation_delay_days,
            'generation_day_of_month': obj.generation_day_of_month,
            'approval_required': obj.approval_required,
            'approver_role': obj.approver_role,
            'review_deadline_days': obj.review_deadline_days,
            'payment_rule': obj.payment_rule,
            'payment_offset_days': obj.payment_offset_days,
            'payment_day_of_month': obj.payment_day_of_month,
            'payment_weekday': obj.payment_weekday,
        }

    @classmethod
    def resolve_schedule(
        cls,
        employee: Optional[Employee] = None,
        centre: Optional[Branch] = None,
        business: Optional[Business] = None
    ) -> Dict[str, Any]:
        """
        Resolves effective payroll schedule following the strict hierarchy:
        EMPLOYEE OVERRIDE -> CENTRE OVERRIDE -> ENTERPRISE DEFAULT -> SYSTEM DEFAULT.
        Returns resolved configuration, source provenance, and raw overrides.
        """
        if employee and not centre:
            centre = employee.branch
        if (employee or centre) and not business:
            business = employee.business if employee else centre.business

        if not business:
            raise ValueError('Cannot resolve schedule without a valid Business context.')

        # 1. Load Enterprise Default
        ent_cfg = PayrollScheduleConfig.objects.filter(
            business=business,
            scope=ScheduleConfigScope.ENTERPRISE,
            is_active=True
        ).first()

        # 2. Load Centre Override
        cen_cfg = None
        if centre:
            cen_cfg = PayrollScheduleConfig.objects.filter(
                business=business,
                centre=centre,
                scope=ScheduleConfigScope.CENTRE,
                is_active=True
            ).first()

        # 3. Load Employee Override
        emp_cfg = None
        if employee:
            emp_cfg = PayrollScheduleConfig.objects.filter(
                business=business,
                employee=employee,
                scope=ScheduleConfigScope.EMPLOYEE,
                is_active=True
            ).first()

        # Start with base defaults
        resolved: Dict[str, Any] = dict(SYSTEM_DEFAULT_SCHEDULE)
        source = 'ENTERPRISE'
        source_display = 'Enterprise Default'
        has_override = False

        if ent_cfg:
            for field in cls.CONFIG_FIELDS:
                val = getattr(ent_cfg, field, None)
                if val is not None:
                    resolved[field] = val

        if cen_cfg and cen_cfg.has_override:
            source = 'CENTRE'
            source_display = 'Centre Override'
            has_override = True
            for field in cls.CONFIG_FIELDS:
                val = getattr(cen_cfg, field, None)
                if val is not None:
                    resolved[field] = val

        if emp_cfg and emp_cfg.has_override:
            source = 'EMPLOYEE'
            source_display = 'Employee Override'
            has_override = True
            for field in cls.CONFIG_FIELDS:
                val = getattr(emp_cfg, field, None)
                if val is not None:
                    resolved[field] = val

        # Compute calculated current period & payment date
        p_start, p_end = cls.calculate_period_boundaries(resolved)
        expected_payment = cls.calculate_expected_payment_date(p_end, resolved)

        active_rec = emp_cfg if (emp_cfg and emp_cfg.has_override) else (cen_cfg if (cen_cfg and cen_cfg.has_override) else ent_cfg)

        return {
            'effective_config': resolved,
            'source': source,
            'source_display': source_display,
            'has_override': has_override,
            'effective_from': str(resolved.get('effective_from')) if resolved.get('effective_from') else str(timezone.now().date()),
            'effective_to': str(resolved.get('effective_to')) if resolved.get('effective_to') else None,
            'change_reason': active_rec.change_reason if active_rec else '',
            'changed_by_name': (active_rec.changed_by.get_full_name() or active_rec.changed_by.email) if (active_rec and active_rec.changed_by) else None,
            'updated_at': active_rec.updated_at.isoformat() if active_rec else None,
            'current_period': {
                'start': str(p_start),
                'end': str(p_end),
                'label': f"{p_start} → {p_end}",
            },
            'expected_payment_date': str(expected_payment),
            'employee_override': cls.serialize_config_dict(emp_cfg),
            'centre_override': cls.serialize_config_dict(cen_cfg),
            'enterprise_default': cls.serialize_config_dict(ent_cfg),
        }

    @classmethod
    def calculate_period_boundaries(
        cls,
        config: Dict[str, Any],
        ref_date: Optional[date] = None
    ) -> Tuple[date, date]:
        """
        Deterministic period boundary calculator supporting:
        - DAILY
        - WEEKLY (configurable week-start day 0=Mon .. 6=Sun)
        - FORTNIGHTLY (consecutive 14-day blocks or semi-monthly 1-15 & 16-end)
        - MONTHLY_CALENDAR (1st to last day of month)
        - MONTHLY_CUSTOM (custom cycle start day with strict month-end clamping)
        - CUSTOM_PERIOD
        Guarantees zero overlapping or missing days.
        """
        ref = ref_date or timezone.now().date()
        freq = config.get('pay_frequency') or PayFrequency.MONTHLY_CALENDAR

        if freq == PayFrequency.DAILY:
            return ref, ref

        if freq == PayFrequency.WEEKLY:
            start_day = int(config.get('week_start_day', 0))
            delta = (ref.weekday() - start_day) % 7
            p_start = ref - timedelta(days=delta)
            p_end = p_start + timedelta(days=6)
            return p_start, p_end

        if freq == PayFrequency.FORTNIGHTLY:
            anchor = config.get('anchor_date')
            if isinstance(anchor, str):
                from datetime import datetime
                try:
                    anchor = datetime.strptime(anchor, '%Y-%m-%d').date()
                except ValueError:
                    anchor = None

            if anchor:
                diff = (ref - anchor).days
                block_index = diff // 14
                p_start = anchor + timedelta(days=block_index * 14)
                p_end = p_start + timedelta(days=13)
                return p_start, p_end
            else:
                # Standard semi-monthly split: 1-15 and 16-end of month
                if ref.day <= 15:
                    p_start = date(ref.year, ref.month, 1)
                    p_end = date(ref.year, ref.month, 15)
                else:
                    last_day = calendar.monthrange(ref.year, ref.month)[1]
                    p_start = date(ref.year, ref.month, 16)
                    p_end = date(ref.year, ref.month, last_day)
                return p_start, p_end

        if freq == PayFrequency.MONTHLY_CUSTOM:
            cycle_start = int(config.get('custom_cycle_start_day', 1))
            if cycle_start <= 1:
                # Behaves like calendar month
                last_day = calendar.monthrange(ref.year, ref.month)[1]
                return date(ref.year, ref.month, 1), date(ref.year, ref.month, last_day)

            # Month-end handling rule: CLAMP_TO_LAST_DAY
            curr_max = calendar.monthrange(ref.year, ref.month)[1]
            effective_cycle_start = min(cycle_start, curr_max)
            if ref.day >= effective_cycle_start:
                start_day = effective_cycle_start
                p_start = date(ref.year, ref.month, start_day)

                ny = ref.year if ref.month < 12 else ref.year + 1
                nm = ref.month + 1 if ref.month < 12 else 1
                next_max = calendar.monthrange(ny, nm)[1]
                next_start_day = min(cycle_start, next_max)
                p_end = date(ny, nm, next_start_day) - timedelta(days=1)
            else:
                py = ref.year if ref.month > 1 else ref.year - 1
                pm = ref.month - 1 if ref.month > 1 else 12
                prev_max = calendar.monthrange(py, pm)[1]
                prev_start_day = min(cycle_start, prev_max)
                p_start = date(py, pm, prev_start_day)

                this_start_day = min(cycle_start, curr_max)
                p_end = date(ref.year, ref.month, this_start_day) - timedelta(days=1)

            return p_start, p_end

        # Default: MONTHLY_CALENDAR or fallback
        last_day = calendar.monthrange(ref.year, ref.month)[1]
        return date(ref.year, ref.month, 1), date(ref.year, ref.month, last_day)

    @classmethod
    def calculate_expected_payment_date(
        cls,
        period_end: date,
        config: Dict[str, Any]
    ) -> date:
        """
        Calculates the expected disbursement date independent of payroll calculation status.
        Supports:
        - SAME_DAY_AS_PERIOD_END
        - DAYS_AFTER_PERIOD_END (offset)
        - SPECIFIED_WEEKDAY (next target day e.g. Friday)
        - DAY_OF_FOLLOWING_MONTH (e.g. 7th of next month, with month-end clamp)
        - MANUAL / CUSTOM_RULE
        """
        rule = config.get('payment_rule') or PaymentScheduleRule.DAY_OF_FOLLOWING_MONTH

        if rule == PaymentScheduleRule.SAME_DAY_AS_PERIOD_END:
            return period_end

        if rule == PaymentScheduleRule.DAYS_AFTER_PERIOD_END:
            offset = int(config.get('payment_offset_days', 5))
            return period_end + timedelta(days=offset)

        if rule == PaymentScheduleRule.SPECIFIED_WEEKDAY:
            target_weekday = int(config.get('payment_weekday', 4))  # 4=Friday
            delta = (target_weekday - period_end.weekday()) % 7
            if delta == 0:
                delta = 7
            return period_end + timedelta(days=delta)

        if rule == PaymentScheduleRule.DAY_OF_FOLLOWING_MONTH:
            target_day = int(config.get('payment_day_of_month', 7))
            ny = period_end.year if period_end.month < 12 else period_end.year + 1
            nm = period_end.month + 1 if period_end.month < 12 else 1
            max_day = calendar.monthrange(ny, nm)[1]
            return date(ny, nm, min(target_day, max_day))

        # Default fallback (e.g. MANUAL or CUSTOM)
        offset = int(config.get('payment_offset_days', 5))
        return period_end + timedelta(days=offset)

    @classmethod
    def generate_recent_periods(
        cls,
        config: Dict[str, Any],
        count: int = 6,
        as_of: Optional[date] = None
    ) -> List[Dict[str, Any]]:
        """
        Generates recent contiguous periods ending on or before the current cycle,
        useful for payroll run selection and preview.
        """
        periods: List[Dict[str, Any]] = []
        cur_ref = as_of or timezone.now().date()

        for _ in range(count):
            p_start, p_end = cls.calculate_period_boundaries(config, cur_ref)
            exp_pay = cls.calculate_expected_payment_date(p_end, config)

            month_name = p_start.strftime('%B %Y')
            label = f"{month_name} ({p_start} to {p_end})"

            periods.append({
                'period_start': str(p_start),
                'period_end': str(p_end),
                'label': label,
                'expected_payment_date': str(exp_pay),
                'pay_frequency': config.get('pay_frequency', 'MONTHLY_CALENDAR'),
            })

            # Move ref date strictly before p_start to get preceding cycle
            cur_ref = p_start - timedelta(days=1)

        return periods

    @classmethod
    def save_schedule(
        cls,
        scope: str,
        business: Business,
        centre: Optional[Branch] = None,
        employee: Optional[Employee] = None,
        data: Optional[Dict[str, Any]] = None,
        user=None,
        reason: str = ''
    ) -> PayrollScheduleConfig:
        """
        Creates or updates a PayrollScheduleConfig with audit trail and history archiving.
        """
        data = data or {}
        reason = reason or data.get('change_reason') or 'Updated payroll schedule configuration'

        with transaction.atomic():
            # Lookup active config
            filter_kwargs = {
                'business': business,
                'scope': scope,
                'is_active': True,
            }
            if scope == ScheduleConfigScope.CENTRE:
                filter_kwargs['centre'] = centre
            elif scope == ScheduleConfigScope.EMPLOYEE:
                filter_kwargs['employee'] = employee
            else:
                filter_kwargs['centre__isnull'] = True
                filter_kwargs['employee__isnull'] = True

            existing = PayrollScheduleConfig.objects.filter(**filter_kwargs).first()

            # Archive existing config to history
            if existing:
                snapshot = cls.serialize_config_dict(existing) or {}
                PayrollScheduleHistory.objects.create(
                    config=existing,
                    business=business,
                    centre=centre,
                    employee=employee,
                    scope=scope,
                    snapshot=snapshot,
                    effective_from=existing.effective_from or timezone.now().date(),
                    effective_to=timezone.now().date() - timedelta(days=1),
                    change_reason=f"Superseded: {reason}",
                    changed_by=user if (user and user.is_authenticated) else None
                )

            # Build defaults / updates
            defaults = {
                'has_override': True,
                'is_active': True,
                'change_reason': reason,
                'changed_by': user if (user and user.is_authenticated) else None,
            }

            field_keys = [
                'compensation_type', 'pay_frequency', 'week_start_day',
                'custom_cycle_start_day', 'anchor_date', 'month_end_rule',
                'generation_mode', 'generation_delay_days', 'generation_day_of_month',
                'approval_required', 'approver_role', 'review_deadline_days',
                'payment_rule', 'payment_offset_days', 'payment_day_of_month', 'payment_weekday',
                'effective_from'
            ]

            for k in field_keys:
                if k in data and data[k] is not None:
                    defaults[k] = data[k]

            if not existing:
                # Merge with system defaults if brand new record
                for sys_k, sys_v in SYSTEM_DEFAULT_SCHEDULE.items():
                    if sys_k not in defaults:
                        defaults[sys_k] = sys_v

                record = PayrollScheduleConfig.objects.create(
                    business=business,
                    centre=centre,
                    employee=employee,
                    scope=scope,
                    **defaults
                )
            else:
                for attr, val in defaults.items():
                    setattr(existing, attr, val)
                existing.save()
                record = existing

            # Audit log
            AuditService.log(
                user_or_request=user,
                action='SAVE_PAYROLL_SCHEDULE_CONFIG',
                entity_type='PayrollScheduleConfig',
                entity_id=str(record.id),
                new_data=cls.serialize_config_dict(record),
                business=business,
                reason=reason
            )

            return record

    @classmethod
    def reset_override(
        cls,
        scope: str,
        business: Business,
        centre: Optional[Branch] = None,
        employee: Optional[Employee] = None,
        user=None,
        reason: str = ''
    ) -> bool:
        """
        Reverts Centre or Employee override to inherit settings from Enterprise / Centre defaults.
        Preserves history and audit log.
        """
        reason = reason or f"Reverted {scope} override to inherit parent defaults"

        with transaction.atomic():
            filter_kwargs = {
                'business': business,
                'scope': scope,
                'is_active': True,
            }
            if scope == ScheduleConfigScope.CENTRE:
                filter_kwargs['centre'] = centre
            elif scope == ScheduleConfigScope.EMPLOYEE:
                filter_kwargs['employee'] = employee
            else:
                return False

            existing = PayrollScheduleConfig.objects.filter(**filter_kwargs).first()
            if not existing:
                return True

            # Snapshot to history
            PayrollScheduleHistory.objects.create(
                config=existing,
                business=business,
                centre=centre,
                employee=employee,
                scope=scope,
                snapshot=cls.serialize_config_dict(existing) or {},
                effective_from=existing.effective_from or timezone.now().date(),
                effective_to=timezone.now().date(),
                change_reason=f"Reset to default: {reason}",
                changed_by=user if (user and user.is_authenticated) else None
            )

            existing.has_override = False
            existing.change_reason = reason
            existing.changed_by = user if (user and user.is_authenticated) else None
            existing.save(update_fields=['has_override', 'change_reason', 'changed_by', 'updated_at'])

            AuditService.log(
                user_or_request=user,
                action='RESET_PAYROLL_SCHEDULE_OVERRIDE',
                entity_type='PayrollScheduleConfig',
                entity_id=str(existing.id),
                business=business,
                reason=reason
            )

            return True

    @classmethod
    def process_scheduled_drafts(
        cls,
        business: Optional[Business] = None,
        as_of_date: Optional[date] = None
    ) -> List[PayrollRun]:
        """
        Generates automatic DRAFT payroll runs for configurations set to:
        - AUTOMATIC_DRAFT_AFTER_PERIOD_END
        - AUTOMATIC_DRAFT_ON_CONFIGURED_DATE
        Strict safety:
        1. Only generates DRAFT status. Never approves, finalizes, or marks paid.
        2. Prevents duplicate runs for the same scope and period.
        3. Never modifies or overwrites FINALIZED payroll runs.
        """
        from apps.payroll.services.payroll_calculation_service import PayrollCalculationService

        today = as_of_date or timezone.now().date()
        generated_runs: List[PayrollRun] = []

        biz_qs = Business.objects.filter(is_active=True)
        if business:
            biz_qs = biz_qs.filter(id=business.id)

        for biz in biz_qs:
            # Check Enterprise schedule
            ent_schedule = cls.resolve_schedule(business=biz)
            cfg = ent_schedule['effective_config']
            mode = cfg.get('generation_mode')

            if mode in [PayrollGenerationMode.AUTOMATIC_DRAFT_AFTER_PERIOD_END, PayrollGenerationMode.AUTOMATIC_DRAFT_ON_CONFIGURED_DATE]:
                p_start, p_end = cls.calculate_period_boundaries(cfg, today)

                should_run = False
                if mode == PayrollGenerationMode.AUTOMATIC_DRAFT_AFTER_PERIOD_END:
                    delay = int(cfg.get('generation_delay_days', 1))
                    if today >= (p_end + timedelta(days=delay)):
                        should_run = True
                elif mode == PayrollGenerationMode.AUTOMATIC_DRAFT_ON_CONFIGURED_DATE:
                    gen_day = int(cfg.get('generation_day_of_month', 1))
                    if today.day >= gen_day:
                        should_run = True

                if should_run:
                    # Prevent duplicate or overwrite of finalized
                    existing_run = PayrollRun.objects.filter(
                        business=biz,
                        centre__isnull=True,
                        period_start=p_start,
                        period_end=p_end
                    ).first()

                    if not existing_run:
                        exp_payment = cls.calculate_expected_payment_date(p_end, cfg)
                        run = PayrollCalculationService.run_batch_payroll(
                            business=biz,
                            period_start=p_start,
                            period_end=p_end,
                            centre=None,
                            approved_by=None
                        )
                        run.status = PayrollRunStatus.DRAFT
                        run.generation_mode = mode
                        run.pay_frequency = cfg.get('pay_frequency', PayFrequency.MONTHLY_CALENDAR)
                        run.expected_payment_date = exp_payment
                        run.save(update_fields=['status', 'generation_mode', 'pay_frequency', 'expected_payment_date', 'updated_at'])
                        generated_runs.append(run)

        return generated_runs
