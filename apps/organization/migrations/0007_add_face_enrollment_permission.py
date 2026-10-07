from django.db import migrations


def add_face_enrollment_permission(apps, schema_editor):
    Permission = apps.get_model('organization', 'Permission')
    Permission.objects.get_or_create(
        key='employee.face_enrollment',
        defaults={
            'name': 'Face Biometric Enrollment',
            'module': 'employees',
            'description': 'Enroll, re-enroll, and revoke employee face recognition biometrics.'
        }
    )


def remove_face_enrollment_permission(apps, schema_editor):
    Permission = apps.get_model('organization', 'Permission')
    Permission.objects.filter(key='employee.face_enrollment').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('organization', '0006_employee_personal_details'),
    ]

    operations = [
        migrations.RunPython(add_face_enrollment_permission, remove_face_enrollment_permission),
    ]
