from apps.payroll.management.commands.run_payroll_scheduler import Command as SchedulerCommand


class Command(SchedulerCommand):
    help = 'Alias for run_payroll_scheduler: Processes payroll generation, deadline closing, and release lifecycle.'
