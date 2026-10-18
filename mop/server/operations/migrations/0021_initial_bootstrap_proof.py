"""Exact bootstrap proof до credential I/O; старое неизвестное не backfill'им."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('operations', '0020_initial_create_cleanup_anchor')]
    operations = [
        migrations.AddField(model_name='createreservation', name='bootstrap_approval_id', field=models.UUIDField(null=True)),
        migrations.AddField(model_name='createreservation', name='bootstrap_version_id', field=models.UUIDField(null=True)),
        migrations.AddField(model_name='createreservation', name='bootstrap_digest', field=models.CharField(max_length=64, blank=True)),
    ]
