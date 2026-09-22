from typing import Optional
from django.db import transaction
from apps.organization.models import Business, Employee


def generate_next_employee_id(business: Business) -> Optional[str]:
    """
    Generates the next sequential employee ID for a business using database-level locking.
    Uses select_for_update() inside an atomic transaction to guarantee concurrency safety
    and prevent race conditions under parallel creation requests.

    Does NOT use MAX() + 1. Never reuses deactivated employee IDs.
    """
    with transaction.atomic():
        # Lock the business row exclusively until transaction commits
        locked_biz = Business.objects.select_for_update().get(id=business.id)
        if not locked_biz.employee_id_enabled:
            return None

        prefix = (locked_biz.employee_id_prefix or 'EMP').strip().upper()
        current_num = locked_biz.employee_id_next_number

        # Format number with at least 3 digits (e.g. EMP001, EMP010, EMP100)
        formatted_id = f"{prefix}{current_num:03d}"

        # Ensure uniqueness within business (in case of legacy gaps)
        while Employee.objects.filter(business=locked_biz, employee_id=formatted_id).exists():
            current_num += 1
            formatted_id = f"{prefix}{current_num:03d}"

        # Increment sequence counter and persist
        locked_biz.employee_id_next_number = current_num + 1
        locked_biz.save(update_fields=['employee_id_next_number', 'updated_at'])

        return formatted_id
