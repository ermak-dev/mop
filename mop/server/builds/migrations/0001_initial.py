"""Новая установка: только metadata общей durable операции, не второй журнал."""
import uuid
from django.db import migrations, models
from django.db.models import Q
import django.utils.timezone


def fk(to, **kwargs):
    return models.ForeignKey(to, on_delete=models.PROTECT, **kwargs)


def identity():
    return [('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False))]


class Migration(migrations.Migration):
    initial = True
    dependencies = [('operations', '0003_image_build'), ('catalog', '0004_safe_retirement'), ('execution', '0001_initial')]
    operations = [
        migrations.CreateModel(name='BuildRun', fields=[
            ('operation', models.OneToOneField('operations.operation', primary_key=True, on_delete=models.PROTECT, serialize=False)),
            ('policy', fk('catalog.projectpolicysnapshot')), ('project', fk('catalog.project')),
            ('origin', models.CharField(max_length=2048)), ('project_name', models.CharField(max_length=128)),
            ('manifest', models.BinaryField()), ('manifest_digest', models.CharField(max_length=64)),
            ('source_commit', models.CharField(max_length=64)), ('source_ref', models.CharField(max_length=256)),
            ('source_digest', models.CharField(max_length=64)), ('mode', models.CharField(max_length=8)),
            ('force_safety', models.BooleanField()), ('planned_at', models.DateTimeField(null=True)),
            ('original_failure', models.CharField(max_length=512, blank=True)),
        ], options={'default_permissions': (), 'constraints': [models.CheckConstraint(condition=Q(mode__in=('missing','update','rebuild')), name='build_finite_mode')]}),
        migrations.CreateModel(name='BuildNodePlan', fields=[('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('build', fk('builds.buildrun')), ('node', fk('catalog.node')),
            ('source_revision', models.PositiveBigIntegerField()), ('driver', models.CharField(max_length=16)),
        ], options={'default_permissions': (), 'constraints': [models.UniqueConstraint(fields=('build','node'), name='build_unique_node')]}),
        migrations.CreateModel(name='AffectedTargetPlan', fields=identity()+[
            ('build', fk('builds.buildrun')), ('assignment', fk('execution.assignment')),
            ('incarnation', fk('execution.executionincarnation')), ('node', fk('catalog.node')),
            ('source_revision', models.PositiveBigIntegerField()), ('generation', models.PositiveBigIntegerField()),
            ('role', models.CharField(max_length=64)), ('bundle_digest', models.CharField(max_length=64)),
            ('grant_id', models.UUIDField()), ('source_spec', models.BinaryField()), ('spec_digest', models.CharField(max_length=64)),
        ], options={'default_permissions': (), 'constraints': [models.UniqueConstraint(fields=('build','assignment'), name='build_unique_target')]}),
        migrations.CreateModel(name='StageIntent', fields=identity()+[
            ('build', fk('builds.buildrun')), ('step', fk('operations.step')), ('token', models.UUIDField()),
            ('key', models.CharField(max_length=80)), ('stage', models.CharField(max_length=16)),
            ('target', fk('builds.affectedtargetplan', null=True)), ('node', fk('catalog.node', null=True)),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
        ], options={'default_permissions': (), 'constraints': [
            models.UniqueConstraint(fields=('build','key'), name='build_stage_key'),
            models.CheckConstraint(condition=Q(stage__in=('clear','wipe','build','restore','announce')), name='build_finite_stage'),
            models.CheckConstraint(condition=Q(stage__in=('clear','wipe','restore'), target__isnull=False, node__isnull=False)
                | Q(stage='build',target__isnull=True,node__isnull=True)
                | Q(stage='announce',target__isnull=True,node__isnull=False), name='build_stage_shape')]}),
        migrations.CreateModel(name='StageReceipt', fields=identity()+[
            ('intent', fk('builds.stageintent')), ('outcome', models.CharField(max_length=16)),
            ('quiescent', models.BooleanField()), ('reason', models.CharField(max_length=512)),
            ('received_at', models.DateTimeField(default=django.utils.timezone.now)),
        ], options={'default_permissions': (), 'constraints': [models.UniqueConstraint(fields=('intent','quiescent'), name='build_receipt_resolution'),
            models.CheckConstraint(condition=Q(outcome='unknown') | Q(outcome__in=('succeeded','failed'), quiescent=True), name='build_receipt_outcome')]}),
    ]
