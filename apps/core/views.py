from django.utils import timezone
from rest_framework import views, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import NotFound

from apps.core.models import Notification
from apps.core.serializers import NotificationSerializer


class NotificationListView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = Notification.objects.filter(user=request.user).order_by('-created_at')[:50]
        return Response(NotificationSerializer(qs, many=True).data)


class NotificationMarkReadView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        notif = Notification.objects.filter(id=pk, user=request.user).first()
        if not notif:
            raise NotFound('Notification not found.')
        notif.is_read = True
        notif.read_at = timezone.now()
        notif.save(update_fields=['is_read', 'read_at'])
        return Response({'detail': 'Notification marked as read.'})


class AuditLogListView(views.APIView):
    """
    SuperAdmin audit log query endpoint.
    Filters by entity_type, action, business_id, actor_id, date range.
    Supports CSV export if format=csv is passed.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.core.permissions import get_user_context
        ctx = get_user_context(request)
        if not ctx['is_superadmin']:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied('Only SuperAdmin can view system audit logs.')

        from apps.core.models import AuditLog
        from apps.core.serializers import AuditLogSerializer
        from django.http import HttpResponse
        import csv

        qs = AuditLog.objects.all().select_related('actor', 'business').order_by('-created_at')

        entity_type = request.query_params.get('entity_type')
        if entity_type and entity_type != 'all':
            qs = qs.filter(entity_type=entity_type)

        action = request.query_params.get('action')
        if action and action != 'all':
            qs = qs.filter(action=action)

        business_id = request.query_params.get('business_id')
        if business_id and business_id != 'all':
            qs = qs.filter(business_id=business_id)

        start_date = request.query_params.get('start_date')
        if start_date:
            qs = qs.filter(created_at__date__gte=start_date)

        end_date = request.query_params.get('end_date')
        if end_date:
            qs = qs.filter(created_at__date__lte=end_date)

        if request.query_params.get('format') == 'csv':
            response = HttpResponse(content_type='text/csv')
            response['Content-Disposition'] = 'attachment; filename="audit_logs.csv"'
            writer = csv.writer(response)
            writer.writerow(['Timestamp', 'Actor', 'Action', 'Entity Type', 'Entity ID', 'Business', 'IP Address'])
            for log in qs[:1000]:
                writer.writerow([
                    log.created_at.isoformat(),
                    log.actor.email if log.actor else 'System',
                    log.action,
                    log.entity_type,
                    log.entity_id,
                    log.business.name if log.business else 'Platform',
                    log.ip_address or ''
                ])
            return response

        limit = min(int(request.query_params.get('limit', 100)), 500)
        return Response(AuditLogSerializer(qs[:limit], many=True).data)

