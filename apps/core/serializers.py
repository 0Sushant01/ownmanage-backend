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
