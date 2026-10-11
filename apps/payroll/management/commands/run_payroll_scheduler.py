import time
from datetime import datetime
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.organization.models import Business
from apps.payroll.services.payroll_lifecycle_service import PayrollLifecycleService


class Command(BaseCommand):
    help = 'Runs the 12:01 AM payroll scanning, draft generation, editing deadline closure, and salary release lifecycle.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--once',
            action='store_true',
            help='Execute a single scan and exit immediately (default for cron jobs).'
        )
        parser.add_argument(
            '--daemon',
            action='store_true',
            help='Run continuously as a daemon, scanning at 12:01 AM local time daily.'
        )
        parser.add_argument(
            '--as-of',
            type=str,
            help='Simulate scan as of specific ISO datetime (e.g. 2026-11-01T00:01:00+05:30).'
        )
        parser.add_argument(
            '--business',
            type=str,
            help='UUID of a specific business to scan.'
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("=== OWNManage Payroll Scheduler Engine ==="))

        biz = None
        if options.get('business'):
            biz = Business.objects.filter(id=options['business']).first()
            if not biz:
                self.stderr.write(self.style.ERROR(f"Business {options['business']} not found."))
                return

        as_of_dt = None
        if options.get('as_of'):
            try:
                as_of_dt = datetime.fromisoformat(options['as_of'])
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"Invalid --as-of timestamp format: {e}"))
                return

        if options.get('daemon'):
            self.stdout.write(self.style.NOTICE("Starting payroll scheduler daemon (checking every 60s for 12:01 AM)..."))
            last_scanned_day = None
            try:
                while True:
                    now = timezone.now()
                    # Check if current local time is 00:01 (12:01 AM) or if day rolled over
                    cur_day = now.date()
                    if cur_day != last_scanned_day:
                        self.stdout.write(f"[{now.isoformat()}] Running scheduled payroll lifecycle scan...")
                        res = PayrollLifecycleService.run_daily_midnight_scan(business=biz)
                        self.stdout.write(self.style.SUCCESS(
                            f"Scan completed: Generated={res['generated_runs_count']}, Finalized={res['finalized_runs_count']}, Released={res['released_runs_count']}"
                        ))
                        last_scanned_day = cur_day
                    time.sleep(60)
            except KeyboardInterrupt:
                self.stdout.write(self.style.WARNING("Scheduler daemon stopped by user."))
                return
        else:
            # Default / --once execution
            self.stdout.write(f"Executing lifecycle scan as of {as_of_dt or timezone.now()}...")
            res = PayrollLifecycleService.run_daily_midnight_scan(as_of_datetime=as_of_dt, business=biz)
            self.stdout.write(self.style.SUCCESS(
                f"Completed: Generated {res['generated_runs_count']} drafts, Finalized {res['finalized_runs_count']} runs, Released {res['released_runs_count']} payslips."
            ))
            if res['generated_run_ids']:
                self.stdout.write(f"  Generated runs: {res['generated_run_ids']}")
            if res['finalized_run_ids']:
                self.stdout.write(f"  Finalized runs: {res['finalized_run_ids']}")
            if res['released_run_ids']:
                self.stdout.write(f"  Released runs: {res['released_run_ids']}")
