import calendar
from datetime import date, datetime, timedelta, time
from typing import Dict, Any, List, Optional
from zoneinfo import ZoneInfo
from django.db import transaction
from django.utils import timezone

from apps.organization.models import Business, Branch, BusinessRole
from apps.payroll.models import (
    PayrollRun, PayrollRunStatus, Payroll, PayrollStatus,
    GenerationType, SalarySlipVisibilityPolicy, PayrollScheduleConfig,
    ScheduleConfigScope
)
from apps.payroll.services.payroll_schedule_service import PayrollScheduleService
from apps.payroll.services.payroll_calculation_service import PayrollCalculationService
from apps.core.services.audit_service import AuditService


class PayrollLifecycleService:
    @staticmethod
    def get_business_timezone(business: Business) -> ZoneInfo:
        tz_name = getattr(business, 'timezone', 'Asia/Kolkata') or 'Asia/Kolkata'
        try:
            return ZoneInfo(tz_name)
        except Exception:
            return ZoneInfo('Asia/Kolkata')

    @classmethod
    def get_local_now(cls, business: Business, as_of_datetime: Optional[datetime] = None) -> datetime:
        tz = cls.get_business_timezone(business)
        if as_of_datetime:
            if timezone.is_naive(as_of_datetime):
                return timezone.make_aware(as_of_datetime, tz)
            return as_of_datetime.astimezone(tz)
        return timezone.now().astimezone(tz)

    @classmethod
    def scan_and_generate_scheduled_payrolls(
        cls,
        as_of_datetime: Optional[datetime] = None,
        business: Optional[Business] = None
    ) -> List[PayrollRun]:
        """
        Scans all active businesses and centres at 12:01 AM (or specified time)
        to identify which payroll periods have completed and generate DRAFT payroll runs.
        Supports DAILY, WEEKLY, and MONTHLY generation schedules.
        Idempotent: Never duplicates an existing payroll run for the same period.
        """
        businesses = [business] if business else Business.objects.filter(is_active=True)
        generated_runs: List[PayrollRun] = []

        for biz in businesses:
            local_dt = cls.get_local_now(biz, as_of_datetime)
            local_date = local_dt.date()

            # Target scopes: Enterprise level and each active branch
            centres = [None] + list(Branch.objects.filter(business=biz, is_active=True))

            for centre in centres:
                try:
                    sched_res = PayrollScheduleService.resolve_schedule(centre=centre, business=biz)
                    eff_cfg = sched_res['effective_config']
                    gen_type = eff_cfg.get('generation_type', GenerationType.MONTHLY)

                    period_start: Optional[date] = None
                    period_end: Optional[date] = None

                    # 1. DAILY GENERATION
                    if gen_type == GenerationType.DAILY:
                        # For daily generation, evaluate completed yesterday or current date
                        period_start = local_date - timedelta(days=1)
                        period_end = period_start

                    # 2. WEEKLY GENERATION
                    elif gen_type == GenerationType.WEEKLY:
                        gen_weekday = int(eff_cfg.get('generation_weekday', 0))
                        # Check if today is the configured generation weekday (0=Mon .. 6=Sun)
                        if local_date.weekday() == gen_weekday:
                            period_start, period_end = PayrollScheduleService.get_completed_weekly_period(
                                gen_weekday, local_date
                            )

                    # 3. MONTHLY GENERATION
                    elif gen_type == GenerationType.MONTHLY:
                        gen_day = int(eff_cfg.get('generation_day_of_month', 1))
                        eff_gen_date = PayrollScheduleService.get_effective_monthly_generation_date(
                            local_date.year, local_date.month, gen_day
                        )
                        # Check if today matches the configured monthly generation date (clamped to month-end)
                        if local_date == eff_gen_date:
                            # Preceding completed calendar month
                            first_of_this_month = date(local_date.year, local_date.month, 1)
                            period_end = first_of_this_month - timedelta(days=1)
                            period_start = date(period_end.year, period_end.month, 1)

                    if period_start and period_end:
                        # Idempotency check: Do not duplicate if already exists
                        exists = PayrollRun.objects.filter(
                            business=biz,
                            centre=centre,
                            period_start=period_start,
                            period_end=period_end
                        ).exists()

                        if not exists:
                            run = PayrollCalculationService.run_batch_payroll(
                                business=biz,
                                period_start=period_start,
                                period_end=period_end,
                                centre=centre,
                                approved_by=None
                            )
                            # Set status to DRAFT so employees cannot see it
                            run.status = PayrollRunStatus.DRAFT
                            run.save(update_fields=['status', 'updated_at'])
                            run.payrolls.update(status=PayrollStatus.DRAFT)

                            generated_runs.append(run)

                            AuditService.log(
                                user_or_request=None,
                                action='SCHEDULED_PAYROLL_GENERATION',
                                entity_type='PayrollRun',
                                entity_id=str(run.id),
                                business=biz,
                                reason=f'Scheduled 12:01 AM generation created {gen_type} draft run for {period_start} to {period_end}'
                            )
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error(
                        f"Error in scheduled payroll generation for business {biz.id}, centre {centre}: {e}"
                    )

        return generated_runs

    @classmethod
    def close_expired_editing_windows_and_finalize(
        cls,
        as_of_datetime: Optional[datetime] = None,
        business: Optional[Business] = None
    ) -> List[PayrollRun]:
        """
        Scans in-flight DRAFT / REVIEW payroll runs whose editing deadline has elapsed.
        Closes the editing window, finalizes the payroll run, locks the attendance period,
        and applies visibility rules.
        """
        ref_dt = as_of_datetime or timezone.now()
        qs = PayrollRun.objects.filter(
            status__in=[PayrollRunStatus.DRAFT, PayrollRunStatus.REVIEW, PayrollRunStatus.APPROVED],
            editing_deadline__lte=ref_dt
        )
        if business:
            qs = qs.filter(business=business)

        finalized_runs: List[PayrollRun] = []
        for run in qs:
            try:
                finalized = PayrollCalculationService.finalize_payroll_run(run)
                finalized_runs.append(finalized)
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Error auto-finalizing payroll run {run.id}: {e}")

        return finalized_runs

    @classmethod
    def release_due_salary_slips(
        cls,
        as_of_datetime: Optional[datetime] = None,
        business: Optional[Business] = None
    ) -> List[PayrollRun]:
        """
        Scans FINALIZED payroll runs with visibility_policy == ON_PAYMENT_DATE.
        If today has reached the expected_payment_date, releases the payroll run
        and makes payslips visible to employees.
        """
        businesses = [business] if business else Business.objects.filter(is_active=True)
        released_runs: List[PayrollRun] = []

        for biz in businesses:
            local_dt = cls.get_local_now(biz, as_of_datetime)
            local_date = local_dt.date()

            qs = PayrollRun.objects.filter(
                business=biz,
                status=PayrollRunStatus.FINALIZED,
                visibility_policy=SalarySlipVisibilityPolicy.ON_PAYMENT_DATE,
                expected_payment_date__lte=local_date
            )
            for run in qs:
                try:
                    rel = PayrollCalculationService.release_payroll_run(run)
                    released_runs.append(rel)
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error(f"Error auto-releasing payroll run {run.id}: {e}")

        return released_runs

    @classmethod
    def run_daily_midnight_scan(
        cls,
        as_of_datetime: Optional[datetime] = None,
        business: Optional[Business] = None
    ) -> Dict[str, Any]:
        """
        Authoritative 12:01 AM daily lifecycle orchestrator:
        1. Scans and generates scheduled salary drafts.
        2. Closes expired editing windows and finalizes completed review periods.
        3. Locks attendance periods associated with finalized payroll runs.
        4. Releases salary slips due on today's payment date.
        """
        gen = cls.scan_and_generate_scheduled_payrolls(as_of_datetime, business)
        fin = cls.close_expired_editing_windows_and_finalize(as_of_datetime, business)
        rel = cls.release_due_salary_slips(as_of_datetime, business)

        return {
            'as_of': (as_of_datetime or timezone.now()).isoformat(),
            'generated_runs_count': len(gen),
            'generated_run_ids': [str(r.id) for r in gen],
            'finalized_runs_count': len(fin),
            'finalized_run_ids': [str(r.id) for r in fin],
            'released_runs_count': len(rel),
            'released_run_ids': [str(r.id) for r in rel],
        }
