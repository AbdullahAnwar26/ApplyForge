from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0005_alter_application_options_application_last_error_and_more'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='jobopportunity',
            constraint=models.UniqueConstraint(
                condition=models.Q(source_url__isnull=False) & ~models.Q(source_url=''),
                fields=('source_url',),
                name='unique_job_source_url',
            ),
        ),
        migrations.AddConstraint(
            model_name='jobopportunity',
            constraint=models.UniqueConstraint(
                condition=models.Q(jd_fingerprint__isnull=False) & ~models.Q(jd_fingerprint=''),
                fields=('jd_fingerprint',),
                name='unique_job_jd_fingerprint',
            ),
        ),
    ]
