#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def main():
    """Run administrative tasks."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    # Gracefully handle initial database connectivity during runserver check_migrations
    try:
        from django.core.management.commands.runserver import Command as RunserverCommand
        from django.db.utils import OperationalError

        orig_check_migrations = RunserverCommand.check_migrations

        def safe_check_migrations(self):
            try:
                return orig_check_migrations(self)
            except OperationalError as err:
                self.stdout.write(
                    self.style.WARNING(
                        f"\n[Supabase DB Notice] Unable to connect to PostgreSQL: {err}\n"
                        "The development server is starting. Update DB_* variables in .env to connect to your Supabase instance.\n"
                    )
                )

        RunserverCommand.check_migrations = safe_check_migrations
    except Exception:
        pass

    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
