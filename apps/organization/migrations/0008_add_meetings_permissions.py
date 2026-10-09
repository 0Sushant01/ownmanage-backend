from django.db import migrations


MEETINGS_PERMISSIONS = [
    {
        'key': 'meetings.view',
        'name': 'View Meetings',
        'module': 'meetings',
        'description': 'View meetings the user is invited to or organized.',
        'default_scope': 'SELF',
    },
    {
        'key': 'meetings.view_all_centre',
        'name': 'View All Centre Meetings',
        'module': 'meetings',
        'description': 'View all meetings scheduled in the assigned centre.',
        'default_scope': 'CENTER',
    },
    {
        'key': 'meetings.view_all_enterprise',
        'name': 'View All Enterprise Meetings',
        'module': 'meetings',
        'description': 'View all meetings scheduled enterprise-wide across all centres.',
        'default_scope': 'ENTERPRISE',
    },
    {
        'key': 'meetings.create',
        'name': 'Schedule Meetings',
        'module': 'meetings',
        'description': 'Schedule and create new meetings.',
        'default_scope': 'CENTER',
    },
    {
        'key': 'meetings.edit_own',
        'name': 'Edit Own Meetings',
        'module': 'meetings',
        'description': 'Edit or reschedule meetings organized by self.',
        'default_scope': 'SELF',
    },
    {
        'key': 'meetings.edit_any',
        'name': 'Edit Any Meeting',
        'module': 'meetings',
        'description': 'Edit or reschedule any meeting within permitted centre or business scope.',
        'default_scope': 'CENTER',
    },
    {
        'key': 'meetings.cancel_own',
        'name': 'Cancel Own Meetings',
        'module': 'meetings',
        'description': 'Cancel meetings organized by self.',
        'default_scope': 'SELF',
    },
    {
        'key': 'meetings.cancel_any',
        'name': 'Cancel Any Meeting',
        'module': 'meetings',
        'description': 'Cancel any meeting within permitted centre or business scope.',
        'default_scope': 'CENTER',
    },
    {
        'key': 'meetings.invite_internal',
        'name': 'Invite Internal Staff',
        'module': 'meetings',
        'description': 'Select and invite internal employees to meetings.',
        'default_scope': 'CENTER',
    },
    {
        'key': 'meetings.invite_external',
        'name': 'Invite External Guests',
        'module': 'meetings',
        'description': 'Add external guest email addresses to meeting invitations.',
        'default_scope': 'CENTER',
    },
    {
        'key': 'meetings.manage_participants',
        'name': 'Manage Participants',
        'module': 'meetings',
        'description': 'Add or remove participants after a meeting has been scheduled.',
        'default_scope': 'CENTER',
    },
]


def add_meetings_permissions(apps, schema_editor):
    Permission = apps.get_model('organization', 'Permission')
    for perm_data in MEETINGS_PERMISSIONS:
        Permission.objects.get_or_create(
            key=perm_data['key'],
            defaults={
                'name': perm_data['name'],
                'module': perm_data['module'],
                'description': perm_data['description'],
                'default_scope': perm_data['default_scope'],
            }
        )


def remove_meetings_permissions(apps, schema_editor):
    Permission = apps.get_model('organization', 'Permission')
    keys = [p['key'] for p in MEETINGS_PERMISSIONS]
    Permission.objects.filter(key__in=keys).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('organization', '0007_add_face_enrollment_permission'),
    ]

    operations = [
        migrations.RunPython(add_meetings_permissions, remove_meetings_permissions),
    ]
