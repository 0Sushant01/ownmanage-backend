import json
from django.core.serializers.json import DjangoJSONEncoder
from apps.core.models import AuditLog


def _make_json_safe(data):
    if data is None:
        return None
    try:
        return json.loads(json.dumps(data, cls=DjangoJSONEncoder))
    except Exception:
        return str(data)


class AuditService:
    @staticmethod
    def log(user_or_request, action: str, entity_type: str, entity_id: str, old_data=None, new_data=None, business=None, reason=''):
        """
        Records an append-only audit trail entry for critical business mutations.
        """
        user = None
        ip = None
        user_agent = ''

        if hasattr(user_or_request, 'user'):
            user = user_or_request.user if user_or_request.user.is_authenticated else None
            ip = user_or_request.META.get('REMOTE_ADDR')
            user_agent = user_or_request.META.get('HTTP_USER_AGENT', '')
        elif user_or_request and hasattr(user_or_request, 'is_authenticated') and user_or_request.is_authenticated:
            user = user_or_request

        if not business and user and hasattr(user, 'business_memberships'):
            membership = user.business_memberships.filter(is_active=True).first()
            if membership:
                business = membership.business

        old_clean = _make_json_safe(old_data)
        new_clean = _make_json_safe(new_data)

        if reason and isinstance(new_clean, dict):
            new_clean['_audit_reason'] = reason

        return AuditLog.objects.create(
            business=business,
            actor=user,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            old_data=old_clean,
            new_data=new_clean,
            ip_address=ip,
            user_agent=user_agent[:500] if user_agent else ''
        )
