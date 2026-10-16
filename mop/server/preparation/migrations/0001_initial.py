"""Пустая установка: schema bootstrap без импорта файлов."""
import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def identity():
    return ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False))


def fk(to, **kwargs):
    return models.ForeignKey(to=to, on_delete=django.db.models.deletion.PROTECT, **kwargs)


def created():
    return ('created_at', models.DateTimeField(default=django.utils.timezone.now))


class Migration(migrations.Migration):
    initial = True
    dependencies = [('catalog', '0002_action_permissions'), ('secrets', '0002_credentialrotation_rotation_pending_enum_and_more'),
                    migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='BootstrapRevision', fields=[identity(),
            ('number', models.PositiveBigIntegerField()), ('digest', models.CharField(max_length=64)),
            ('provenance', models.CharField(max_length=1024)), ('commit', models.CharField(max_length=64)),
            ('task_size', models.PositiveIntegerField()), ('vars_size', models.PositiveIntegerField()), created(),
            ('project', fk('catalog.project')), ('owner', fk(settings.AUTH_USER_MODEL)),
            ('content_version', models.OneToOneField(to='secrets.secretversion', on_delete=django.db.models.deletion.PROTECT))],
            options={'default_permissions': (), 'permissions': [('approve_bootstrap', 'Can approve exact bootstrap bytes')],
                'constraints': [models.UniqueConstraint(fields=('project', 'owner', 'number'), name='prep_revision_number'),
                    models.CheckConstraint(condition=models.Q(number__gte=1, task_size__lte=262144, vars_size__lte=131072), name='prep_revision_sizes')]}),
        migrations.CreateModel(name='BootstrapApproval', fields=[identity(), created(), ('human', fk(settings.AUTH_USER_MODEL)),
            ('revision', models.OneToOneField(to='preparation.bootstraprevision', on_delete=django.db.models.deletion.PROTECT))], options={'default_permissions': ()}),
        migrations.CreateModel(name='ApprovalRevocation', fields=[identity(), created(),
            ('approval', models.OneToOneField(to='preparation.bootstrapapproval', on_delete=django.db.models.deletion.PROTECT))], options={'default_permissions': ()}),
        migrations.CreateModel(name='BootstrapRun', fields=[identity(),
            ('target_id', models.UUIDField()), ('incarnation', models.UUIDField()), ('request_id', models.UUIDField()),
            ('version', models.PositiveBigIntegerField(default=1)), ('state', models.CharField(default='accepted', max_length=16)),
            ('failures', models.PositiveIntegerField(default=0)), ('failure_limit', models.PositiveIntegerField(default=3)),
            ('deadline', models.DateTimeField()), ('started_at', models.DateTimeField(null=True)), ('finished_at', models.DateTimeField(null=True)), created(),
            ('approval', fk('preparation.bootstrapapproval'))], options={'default_permissions': (), 'constraints': [
                models.UniqueConstraint(fields=('target_id', 'incarnation', 'request_id'), name='prep_run_request'),
                models.CheckConstraint(condition=models.Q(version__gte=1, failures__lte=10, failure_limit__gte=1, failure_limit__lte=10), name='prep_run_counters'),
                models.CheckConstraint(condition=models.Q(state__in=('accepted', 'running', 'succeeded', 'failed', 'refused', 'unknown', 'gave_up')), name='prep_run_state')]}),
        migrations.CreateModel(name='BootstrapStep', fields=[identity(), ('ordinal', models.PositiveIntegerField()),
            ('name', models.CharField(max_length=64)), ('state', models.CharField(default='pending', max_length=16)),
            ('attempt', models.PositiveIntegerField(default=0)), ('failures', models.PositiveIntegerField(default=0)),
            ('log', models.TextField(blank=True)), ('log_bytes', models.PositiveIntegerField(default=0)),
            ('progress', models.PositiveIntegerField(default=0)), ('started_at', models.DateTimeField(null=True)), ('finished_at', models.DateTimeField(null=True)),
            ('run', fk('preparation.bootstraprun', related_name='steps'))], options={'default_permissions': (), 'constraints': [
                models.UniqueConstraint(fields=('run', 'ordinal'), name='prep_step_ordinal'),
                models.CheckConstraint(condition=models.Q(ordinal__lt=64, attempt__lte=10, failures__lte=10, log_bytes__lte=4096, progress__lte=100), name='prep_step_bounds'),
                models.CheckConstraint(condition=models.Q(state__in=('pending', 'running', 'succeeded', 'failed', 'refused', 'unknown')), name='prep_step_state')]}),
    ]
