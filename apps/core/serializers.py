from rest_framework import serializers
from apps.core.models import Notification, Announcement, AuditLog


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ['id', 'title', 'message', 'notification_type', 'is_read', 'read_at', 'created_at']
        read_only_fields = ['id', 'title', 'message', 'notification_type', 'created_at']


class AnnouncementSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True, default='')

    class Meta:
        model = Announcement
        fields = ['id', 'title', 'message', 'created_by_name', 'published_at', 'expires_at', 'is_active', 'created_at']


class AuditLogSerializer(serializers.ModelSerializer):
    actor_email = serializers.EmailField(source='actor.email', read_only=True, default='System')
    actor_name = serializers.CharField(source='actor.full_name', read_only=True, default='System')
    business_name = serializers.CharField(source='business.name', read_only=True, default='Platform')

    class Meta:
        model = AuditLog
        fields = [
            'id', 'business', 'business_name', 'actor', 'actor_email', 'actor_name',
            'action', 'entity_type', 'entity_id', 'old_data', 'new_data',
            'ip_address', 'user_agent', 'created_at'
        ]
        read_only_fields = fields
