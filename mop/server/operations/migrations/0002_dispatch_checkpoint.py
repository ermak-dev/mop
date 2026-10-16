"""Dispatch authorization отдельно от claim; безопасная причина не теряется."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('operations', '0001_initial')]
    operations = [
        migrations.AddField(model_name='step', name='dispatched_at', field=models.DateTimeField(null=True)),
        migrations.AddField(model_name='operation', name='reason', field=models.CharField(blank=True, default='', max_length=512), preserve_default=False),
        migrations.AddField(model_name='auditevent', name='reason', field=models.CharField(blank=True, default='', max_length=512), preserve_default=False),
    ]
