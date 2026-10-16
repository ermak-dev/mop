"""Новая установка: назначения и receipts, без legacy import/state migration."""
import uuid
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q, F
import django.utils.timezone


def identity():
    return [('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('revision', models.PositiveBigIntegerField(default=1))]


def fk(target, **kwargs):
    return models.ForeignKey(target, on_delete=models.PROTECT, **kwargs)


def one(target, **kwargs):
    return models.OneToOneField(target, on_delete=models.PROTECT, **kwargs)


def options(name, *constraints, permissions=None):
    value = {'abstract': False, 'constraints': [models.CheckConstraint(
        condition=Q(revision__gte=1), name=name.lower() + '_revision_positive'), *constraints]}
    if permissions is not None:
        value['permissions'] = permissions
    return value


class Migration(migrations.Migration):
    initial = True
    dependencies = [('catalog', '0002_action_permissions'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='Assignment', fields=identity()+[
            ('project', fk('catalog.project')), ('node', fk('catalog.node')),
            ('owner', fk(settings.AUTH_USER_MODEL, null=True)),
            ('generation', models.PositiveBigIntegerField(default=1)),
            ('role', models.CharField(max_length=64)), ('bundle_digest', models.CharField(max_length=64)),
            ('grant_id', models.UUIDField()), ('expires_at', models.DateTimeField()),
        ], options=options('assignment', models.CheckConstraint(condition=Q(generation__gte=1),
            name='assignment_generation_positive'), permissions=[('assign_work', 'Can assign human work')])),
        migrations.CreateModel(name='ExecutionIncarnation', fields=identity()+[
            ('assignment', fk('execution.assignment')), ('node', fk('catalog.node')),
            ('executor_epoch', models.UUIDField()), ('accepting', models.BooleanField(default=True)),
            ('replaced_by_epoch', models.UUIDField(null=True)),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
        ], options=options('executionincarnation', models.CheckConstraint(
            condition=Q(accepting=False) | Q(replaced_by_epoch__isnull=True),
            name='incarnation_accepting_not_replaced'))),
        migrations.AddField(model_name='assignment', name='current_incarnation',
            field=fk('execution.executionincarnation', null=True, related_name='+')),
        migrations.CreateModel(name='AssignmentAttempt', fields=identity()+[
            ('assignment', fk('execution.assignment')), ('incarnation', fk('execution.executionincarnation')),
            ('principal_id', models.UUIDField()), ('request_id', models.UUIDField()), ('epoch', models.UUIDField()),
            ('operation_id', models.UUIDField()), ('step_id', models.UUIDField()),
            ('executor_epoch', models.UUIDField()), ('owner', fk(settings.AUTH_USER_MODEL)),
            ('generation', models.PositiveBigIntegerField()), ('assignment_revision', models.PositiveBigIntegerField()),
            ('role', models.CharField(max_length=64)), ('bundle_digest', models.CharField(max_length=64)),
            ('grant_id', models.UUIDField()), ('action', models.CharField(max_length=32)),
            ('not_after', models.DateTimeField()), ('lease_until', models.DateTimeField()),
            ('state', models.CharField(max_length=16, default='claimed')),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
        ], options=options('assignmentattempt',
            models.UniqueConstraint(fields=('principal_id', 'request_id'), name='attempt_request_identity'),
            models.UniqueConstraint(fields=('operation_id', 'step_id'), name='attempt_operation_step'),
            models.UniqueConstraint(fields=('assignment',), condition=Q(state__in=('claimed', 'unknown')),
                name='attempt_one_unresolved_resource'),
            models.CheckConstraint(condition=Q(generation__gte=1, assignment_revision__gte=1), name='attempt_admitted_counters'),
            models.CheckConstraint(condition=Q(state__in=('claimed', 'unknown', 'succeeded', 'failed')), name='attempt_known_state'),
            models.CheckConstraint(condition=Q(action__in=('restart_session', 'apply_identity', 'send_message', 'confirm_ready')),
                name='attempt_named_action'),
            models.CheckConstraint(condition=Q(lease_until__lte=F('not_after')), name='attempt_lease_within_deadline'),
            permissions=[('poll_work', 'Can poll enrolled machine work'), ('claim_work', 'Can claim enrolled machine work'),
                ('report_result', 'Can report enrolled machine result'), ('read_facts', 'Can read enrolled machine facts'),
                ('reconcile_work', 'Can reconcile unknown machine work')])),
        migrations.CreateModel(name='ResultReceipt', fields=identity()+[
            ('attempt', fk('execution.assignmentattempt')),
            ('expected_revision', models.PositiveBigIntegerField()), ('resulting_revision', models.PositiveBigIntegerField()),
            ('outcome', models.CharField(max_length=16)), ('evidence', models.CharField(max_length=32)),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
        ], options=options('resultreceipt',
            models.CheckConstraint(condition=Q(expected_revision__gte=1, resulting_revision=F('expected_revision')+1),
                name='receipt_revision_successor'),
            models.CheckConstraint(condition=Q(outcome__in=('succeeded', 'failed', 'unknown')), name='execution_receipt_known_outcome'))),
        migrations.CreateModel(name='IdentityApplication', fields=identity()+[
            ('attempt', one('execution.assignmentattempt')), ('receipt', one('execution.resultreceipt')),
        ], options=options('identityapplication')),
        migrations.CreateModel(name='ReadinessReceipt', fields=identity()+[
            ('attempt', one('execution.assignmentattempt')), ('receipt', one('execution.resultreceipt')),
        ], options=options('readinessreceipt')),
        migrations.CreateModel(name='LandingToken', fields=identity()+[
            ('project', one('catalog.project')), ('owner', fk(settings.AUTH_USER_MODEL, null=True)),
            ('generation', models.PositiveBigIntegerField(default=1)),
        ], options=options('landingtoken', models.CheckConstraint(condition=Q(generation__gte=1),
            name='landing_generation_positive'), permissions=[('acquire_landing', 'Can acquire human landing token'),
                ('release_landing', 'Can release own landing token')])),
    ]
