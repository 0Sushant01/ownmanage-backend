from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('organization', '0005_branch_currency_branch_email_branch_geofence_radius_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='employee',
            name='date_of_birth',
            field=models.DateField(blank=True, null=True, verbose_name='Date of Birth'),
        ),
        migrations.AddField(
            model_name='employee',
            name='address',
            field=models.TextField(blank=True, verbose_name='Address'),
        ),
        migrations.AddField(
            model_name='employee',
            name='emergency_contact',
            field=models.CharField(blank=True, max_length=100, verbose_name='Emergency Contact'),
        ),
    ]
