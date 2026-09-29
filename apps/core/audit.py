from apps.core.models import AuditLog


def get_client_ip(request):
    if not request:
        return None
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def record_audit_log(
    action: str,
    entity_type: str,
    entity_id: str,
    actor=None,
    business=None,
    old_data=None,
    new_data=None,
    request=None
) -> AuditLog:
    """
    Safely creates an immutable audit log record for security, administrative,
    and financial actions. Never raises an exception that would break the primary operation.
    """
    try:
        ip_address = get_client_ip(request) if request else None
        user_agent = request.META.get('HTTP_USER_AGENT', '')[:500] if request else ''

        if actor is None and request and hasattr(request, 'user') and request.user.is_authenticated:
            actor = request.user

        log = AuditLog.objects.create(
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            actor=actor if actor and actor.is_authenticated else None,
            business=business,
            old_data=old_data,
            new_data=new_data,
            ip_address=ip_address,
            user_agent=user_agent
        )
        return log
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to record audit log: {e}")
        return None
