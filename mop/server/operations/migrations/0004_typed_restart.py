"""Typed target и фиксированные stages общего Operation, без JSON workflow."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('operations','0003_operation_control_permissions')]
    operations = [
        migrations.AlterField(model_name='operation', name='eligible', field=models.BooleanField(null=True)),
        migrations.AddField(model_name='operation', name='target_kind', field=models.CharField(default='node', max_length=16)),
        migrations.AddField(model_name='operation', name='target_id', field=models.UUIDField(null=True)),
        migrations.AddField(model_name='operation', name='incarnation_id', field=models.UUIDField(null=True)),
        migrations.RemoveConstraint(model_name='operation', name='operation_finite_action'),
        migrations.AddConstraint(model_name='operation', constraint=models.CheckConstraint(
            condition=models.Q(action__in=('drain','enable','restart')), name='operation_finite_action')),
        migrations.AddConstraint(model_name='operation', constraint=models.CheckConstraint(
            condition=(models.Q(target_kind='node', action__in=('drain','enable'), eligible__isnull=False,
                target_id__isnull=True, incarnation_id__isnull=True) | models.Q(target_kind='puppet', action='restart',
                eligible__isnull=True, target_id__isnull=False, incarnation_id__isnull=False)), name='operation_typed_target')),
        migrations.AlterField(model_name='step', name='operation', field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,to='operations.operation')),
        migrations.AddField(model_name='step', name='ordinal', field=models.PositiveSmallIntegerField(default=1)),
        migrations.AddField(model_name='step', name='attempt_id', field=models.UUIDField(null=True)),
        migrations.AddField(model_name='step', name='outcome', field=models.CharField(blank=True,max_length=16)),
        migrations.AddField(model_name='step', name='evidence', field=models.CharField(blank=True,max_length=32)),
        migrations.RemoveConstraint(model_name='step', name='step_finite_handler'),
        migrations.AddConstraint(model_name='step', constraint=models.CheckConstraint(
            condition=models.Q(name__in=('node_eligibility','apply_identity','restart_session','confirm_ready')),name='step_finite_handler')),
        migrations.AddConstraint(model_name='step', constraint=models.UniqueConstraint(fields=('operation','ordinal'),name='operation_step_ordinal')),
        migrations.AddConstraint(model_name='step', constraint=models.CheckConstraint(condition=models.Q(outcome__in=('','succeeded','failed','unknown')),name='step_finite_outcome')),
        migrations.CreateModel(name='RestartBinding', fields=[
            ('operation', models.OneToOneField(primary_key=True, serialize=False, on_delete=django.db.models.deletion.PROTECT,to='operations.operation')),
            ('project', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,to='catalog.project')),
            ('executor_epoch', models.UUIDField()), ('owner_id', models.PositiveBigIntegerField()),
            ('generation', models.PositiveBigIntegerField()), ('role', models.CharField(max_length=64)),
            ('bundle_digest', models.CharField(max_length=64)), ('grant_id',models.UUIDField())
        ], options={'default_permissions': ()}),
    ]
