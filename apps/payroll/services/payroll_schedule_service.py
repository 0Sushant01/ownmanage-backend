import calendar
from datetime import date, datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from apps.core.services.audit_service import AuditService
from apps.organization.models import Business, Branch, Employee
from apps.payroll.models import (
    PayrollScheduleConfig, PayrollScheduleHistory, ScheduleConfigScope,
    CompensationType, PayFrequency, GenerationType, MonthEndRule,
    PayrollGenerationMode, PaymentScheduleRule,
    PayrollRun, PayrollRunStatus, PayrollStatus
)


SYSTEM_DEFAULT_SCHEDULE: Dict[str, Any] = {
    'generation_type': GenerationType.MONTHLY,
    'generation_weekday': 0,  # Monday
    'generation_day_of_month': 1,
    'compensation_type': CompensationType.MONTHLY_SALARY,
    'pay_frequency': PayFrequency.MONTHLY_CALENDAR,
    'week_start_day': 0,  # Monday
    'custom_cycle_start_day': 1,
    'anchor_date': None,
    'month_end_rule': MonthEndRule.CLAMP_TO_LAST_DAY,
    'generation_mode': PayrollGenerationMode.MANUAL,
    'generation_delay_days': 1,
    'approval_required': True,
    'approver_role': 'BUSINESS_ADMIN',
    'review_deadline_days': 3,
    'editable_period_duration': 3,
    'editing_period_unit': 'DAYS',
    'finalization_deadline_days': 3,
    'visibility_policy': 'ON_FINALIZATION',
    'payment_rule': PaymentScheduleRule.DAY_OF_FOLLOWING_MONTH,
    'payment_offset_days': 5,
    'payment_day_of_month': 7,
    'payment_weekday': 4,  # Friday
}


class PayrollScheduleService:
    CONFIG_FIELDS = [
        'generation_type', 'generation_weekday', 'generation_day_of_month',
        'compensation_type', 'pay_frequency', 'week_start_day',
        'custom_cycle_start_day', 'anchor_date', 'month_end_rule',
        'generation_mode', 'generation_delay_days',
        'approval_required', 'approver_role', 'review_deadline_days',
        'editable_period_duration', 'editing_period_unit',
        'finalization_deadline_days', 'visibility_policy',
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
            'generation_type': getattr(obj, 'generation_type', None) or (
                GenerationType.DAILY if obj.pay_frequency == PayFrequency.DAILY else (
                    GenerationType.WEEKLY if obj.pay_frequency == PayFrequency.WEEKLY else GenerationType.MONTHLY
                )
            ),
            'generation_weekday': getattr(obj, 'generation_weekday', 0) if getattr(obj, 'generation_weekday', None) is not None else (obj.week_start_day or 0),
            'generation_day_of_month': obj.generation_day_of_month or 1,
            'generation_date': obj.generation_day_of_month or 1,
            'compensation_type': obj.compensation_type,
            'pay_frequency': obj.pay_frequency,
            'week_start_day': obj.week_start_day,
            'custom_cycle_start_day': obj.custom_cycle_start_day,
            'anchor_date': str(obj.anchor_date) if obj.anchor_date else None,
            'month_end_rule': obj.month_end_rule,
            'generation_mode': obj.generation_mode,
            'generation_delay_days': obj.generation_delay_days,
            'approval_required': obj.approval_required,
            'approver_role': obj.approver_role,
            'review_deadline_days': obj.review_deadline_days,
            'editable_period_duration': getattr(obj, 'editable_period_duration', 3),
            'editing_period_unit': getattr(obj, 'editing_period_unit', 'DAYS'),
            'finalization_deadline_days': getattr(obj, 'finalization_deadline_days', 3),
            'visibility_policy': getattr(obj, 'visibility_policy', 'ON_FINALIZATION'),
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
                    resolved[field] = str(val) if isinstance(val, (date, datetime)) else val

        if cen_cfg and cen_cfg.has_override:
            source = 'CENTRE'
            source_display = 'Centre Override'
            has_override = True
            for field in cls.CONFIG_FIELDS:
                val = getattr(cen_cfg, field, None)
                if val is not None:
                    resolved[field] = str(val) if isinstance(val, (date, datetime)) else val

        if emp_cfg and emp_cfg.has_override:
            source = 'EMPLOYEE'
            source_display = 'Employee Override'
            has_override = True
            for field in cls.CONFIG_FIELDS:
                val = getattr(emp_cfg, field, None)
                if val is not None:
                    resolved[field] = str(val) if isinstance(val, (date, datetime)) else val

        # Guarantee generation_type consistency across hierarchy
        g_type = resolved.get('generation_type')
        if not g_type:
            pf = resolved.get('pay_frequency')
            if pf == PayFrequency.DAILY:
                g_type = GenerationType.DAILY
            elif pf == PayFrequency.WEEKLY:
                g_type = GenerationType.WEEKLY
            else:
                g_type = GenerationType.MONTHLY
        resolved['generation_type'] = g_type
        resolved['generation_weekday'] = int(resolved.get('generation_weekday') if resolved.get('generation_weekday') is not None else resolved.get('week_start_day', 0))
        resolved['generation_date'] = int(resolved.get('generation_day_of_month') or 1)
        resolved['generation_day_of_month'] = resolved['generation_date']

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
        gen_type = config.get('generation_type')
        freq = config.get('pay_frequency') or PayFrequency.MONTHLY_CALENDAR

        if gen_type == GenerationType.DAILY or freq == PayFrequency.DAILY:
            return ref, ref

        if gen_type == GenerationType.WEEKLY or freq == PayFrequency.WEEKLY:
            start_day = int(config.get('generation_weekday') if config.get('generation_weekday') is not None else config.get('week_start_day', 0))
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
    def get_effective_monthly_generation_date(cls, year: int, month: int, configured_day: int) -> date:
        """
        Deterministic month-end rule: Clamps configured_day (1-31) to actual last day of month.
        e.g., 31 in Feb 2026 -> 2026-02-28; 31 in Feb 2028 (leap year) -> 2028-02-29; 31 in April -> 2026-04-30.
        """
        last_day = calendar.monthrange(year, month)[1]
        day = min(max(1, int(configured_day or 1)), last_day)
        return date(year, month, day)

    @classmethod
    def get_completed_weekly_period(cls, generation_weekday: int, ref_date: Optional[date] = None) -> Tuple[date, date]:
        """
        Calculates the completed 7-day weekly period that completed on or before the generation weekday.
        If generation day is Monday (0), completed week is preceding Monday to Sunday (ending yesterday).
        Guarantees non-overlapping, contiguous 7-day boundaries.
        """
        ref = ref_date or timezone.now().date()
        gen_day = int(generation_weekday or 0)
        delta = (ref.weekday() - gen_day) % 7
        most_recent_gen_date = ref - timedelta(days=delta)
        completed_end = most_recent_gen_date - timedelta(days=1)
        completed_start = completed_end - timedelta(days=6)
        return completed_start, completed_end

    @classmethod
    def calculate_editing_deadline(
        cls,
        config: Dict[str, Any],
        reference_dt=None
    ):
        """
        Calculates the exact timestamp when editing for a payroll run closes.
        Based on editable_period_duration and editing_period_unit ('DAYS' or 'HOURS').
        """
        ref = reference_dt or timezone.now()
        duration = int(config.get('editable_period_duration', 3))
        unit = str(config.get('editing_period_unit', 'DAYS')).upper()
        if unit == 'HOURS':
            return ref + timedelta(hours=duration)
        return ref + timedelta(days=duration)

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

        # Determine and validate generation_type (Support exactly DAILY, WEEKLY, MONTHLY)
        gen_type = data.get('generation_type')
        if not gen_type:
            pf = data.get('pay_frequency')
            if pf in [PayFrequency.DAILY, 'DAILY']:
                gen_type = GenerationType.DAILY
            elif pf in [PayFrequency.WEEKLY, 'WEEKLY']:
                gen_type = GenerationType.WEEKLY
            else:
                gen_type = GenerationType.MONTHLY

        if gen_type not in [GenerationType.DAILY, GenerationType.WEEKLY, GenerationType.MONTHLY]:
            raise ValidationError(f'Invalid generation type "{gen_type}". Must be DAILY, WEEKLY, or MONTHLY.')

        # Strict validation per generation type
        if gen_type == GenerationType.WEEKLY:
            gen_weekday = data.get('generation_weekday')
            if gen_weekday is None:
                gen_weekday = data.get('week_start_day', 0)
            try:
                gen_weekday = int(gen_weekday)
                if not (0 <= gen_weekday <= 6):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValidationError({'generation_weekday': 'Generation weekday must be between 0 (Monday) and 6 (Sunday).'})
            data['generation_weekday'] = gen_weekday
            data['week_start_day'] = gen_weekday
            data['pay_frequency'] = PayFrequency.WEEKLY

        elif gen_type == GenerationType.MONTHLY:
            gen_date = data.get('generation_date') or data.get('generation_day_of_month', 1)
            try:
                gen_date = int(gen_date)
                if not (1 <= gen_date <= 31):
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValidationError({'generation_date': 'Generation date must be between 1 and 31.'})
            data['generation_day_of_month'] = gen_date
            data['pay_frequency'] = PayFrequency.MONTHLY_CALENDAR

        elif gen_type == GenerationType.DAILY:
            data['pay_frequency'] = PayFrequency.DAILY
            data['generation_weekday'] = 0
            data['generation_day_of_month'] = 1
            if employee:
                current_sched = cls.resolve_schedule(employee=employee)
                emp_comp_type = data.get('compensation_type') or current_sched['effective_config'].get('compensation_type')
                if emp_comp_type == CompensationType.FIXED_CONTRACT:
                    raise ValidationError('Daily salary slip generation is not supported for Fixed Contract compensation.')

        data['generation_type'] = gen_type

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
                'generation_type', 'generation_weekday', 'generation_day_of_month',
                'compensation_type', 'pay_frequency', 'week_start_day',
                'custom_cycle_start_day', 'anchor_date', 'month_end_rule',
                'generation_mode', 'generation_delay_days',
                'approval_required', 'approver_role', 'review_deadline_days',
                'editable_period_duration', 'editing_period_unit',
                'finalization_deadline_days', 'visibility_policy',
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
            gen_type = cfg.get('generation_type') or GenerationType.MONTHLY

            should_run = False
            p_start, p_end = None, None

            if gen_type == GenerationType.DAILY:
                # Daily draft generation for completed yesterday
                yesterday = today - timedelta(days=1)
                p_start, p_end = yesterday, yesterday
                should_run = True

            elif gen_type == GenerationType.WEEKLY:
                target_weekday = int(cfg.get('generation_weekday', 0))
                if today.weekday() == target_weekday:
                    p_start, p_end = cls.get_completed_weekly_period(target_weekday, today)
                    should_run = True

            elif gen_type == GenerationType.MONTHLY:
                gen_date_cfg = int(cfg.get('generation_day_of_month') or 1)
                eff_gen_date = cls.get_effective_monthly_generation_date(today.year, today.month, gen_date_cfg)
                if today >= eff_gen_date:
                    # Completed period is previous calendar month
                    if today.month == 1:
                        py, pm = today.year - 1, 12
                    else:
                        py, pm = today.year, today.month - 1
                    last_day = calendar.monthrange(py, pm)[1]
                    p_start, p_end = date(py, pm, 1), date(py, pm, last_day)
                    should_run = True

            # Also check legacy generation mode triggers if not handled above
            if not should_run and mode in [PayrollGenerationMode.AUTOMATIC_DRAFT_AFTER_PERIOD_END, PayrollGenerationMode.AUTOMATIC_DRAFT_ON_CONFIGURED_DATE]:
                p_start, p_end = cls.calculate_period_boundaries(cfg, today)
                if mode == PayrollGenerationMode.AUTOMATIC_DRAFT_AFTER_PERIOD_END:
                    delay = int(cfg.get('generation_delay_days', 1))
                    if today >= (p_end + timedelta(days=delay)):
                        should_run = True
                elif mode == PayrollGenerationMode.AUTOMATIC_DRAFT_ON_CONFIGURED_DATE:
                    gen_day = int(cfg.get('generation_day_of_month', 1))
                    if today.day >= gen_day:
                        should_run = True

            if should_run and p_start and p_end:
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
                    run.generation_mode = mode or PayrollGenerationMode.MANUAL
                    run.pay_frequency = cfg.get('pay_frequency', PayFrequency.MONTHLY_CALENDAR)
                    run.expected_payment_date = exp_payment
                    run.save(update_fields=['status', 'generation_mode', 'pay_frequency', 'expected_payment_date', 'updated_at'])
                    generated_runs.append(run)

        return generated_runs
