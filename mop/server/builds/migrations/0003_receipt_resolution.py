"""Quiescent процесс и известный исход — разные факты."""
from django.db import migrations, models


def resolve_known(apps, schema_editor):
    apps.get_model('builds','StageReceipt').objects.filter(outcome__in=('succeeded','failed'), quiescent=True).update(resolved=True)


class Migration(migrations.Migration):
    dependencies = [('builds','0002_dispatch_and_skip')]
    operations = [
        migrations.RemoveConstraint(model_name='stagereceipt', name='build_receipt_resolution'),
        migrations.AddField(model_name='stagereceipt', name='resolved', field=models.BooleanField(default=False)),
        migrations.RunPython(resolve_known, migrations.RunPython.noop),
        migrations.AddConstraint(model_name='stagereceipt', constraint=models.UniqueConstraint(fields=('intent','resolved'), name='build_receipt_resolution')),
        migrations.AddConstraint(model_name='stagereceipt', constraint=models.CheckConstraint(condition=
            models.Q(resolved=False,outcome='unknown') | models.Q(resolved=True,outcome__in=('succeeded','failed'),quiescent=True), name='build_receipt_resolved_fact')),
    ]
