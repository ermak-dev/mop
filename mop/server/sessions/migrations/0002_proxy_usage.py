"""#656: самостоятельный leaf proxy usage для новой установки."""
import uuid
import django.utils.timezone
from django.db import migrations, models
from django.db.models import Q


def identity():
    return [('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False))]


def source():
    return ('source', models.ForeignKey(on_delete=models.PROTECT, to='execution.usagesource'))


class Migration(migrations.Migration):
    dependencies = [('execution', '0001_initial')]
    operations = [
        migrations.CreateModel(name='UsageSource', fields=identity()+[
            ('remote_id', models.UUIDField(unique=True)), ('boundary_id', models.UUIDField()),
            ('profile', models.CharField(max_length=64)), ('installed_version', models.CharField(max_length=32)),
            ('enabled', models.BooleanField(default=True)),
            ('source_complete', models.BooleanField(default=False, editable=False)),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
        ], options={'default_permissions': (), 'permissions': [('view_usage', 'Can read installation-wide proxy usage')],
            'constraints': [models.CheckConstraint(condition=Q(source_complete=False), name='usage_source_never_complete')]}),
        migrations.CreateModel(name='UsageReadIntent', fields=identity()+[source(),
            ('owner_id', models.UUIDField()), ('boundary_id', models.UUIDField()),
            ('count', models.PositiveSmallIntegerField()), ('state', models.CharField(default='reading', max_length=16)),
            ('started_at', models.DateTimeField(default=django.utils.timezone.now)),
            ('finished_at', models.DateTimeField(null=True)), ('gap_code', models.CharField(default='', max_length=32)),
            ('batch_fingerprint', models.CharField(default='', max_length=64)),
            ('accepted', models.PositiveSmallIntegerField(default=0)),
            ('duplicates', models.PositiveSmallIntegerField(default=0)),
            ('conflicts', models.PositiveSmallIntegerField(default=0)),
            ('rejected', models.PositiveSmallIntegerField(default=0)),
        ], options={'default_permissions': (), 'constraints': [
            models.UniqueConstraint(fields=('source',), condition=Q(state__in=('reading', 'unknown')),
                                    name='usage_one_unresolved_read'),
            models.CheckConstraint(condition=Q(count__gte=1, count__lte=100), name='usage_read_count_bound'),
            models.CheckConstraint(condition=Q(state__in=('reading', 'unknown', 'committed', 'gap')), name='usage_intent_state')],
            'indexes': [models.Index(fields=['source', 'started_at'], name='usage_intent_source_time')]}),
        migrations.CreateModel(name='UsageEvent', fields=identity()+[source(),
            ('intent', models.ForeignKey(on_delete=models.PROTECT, to='execution.usagereadintent')),
            ('event_id', models.CharField(max_length=64)), ('fingerprint', models.CharField(max_length=64)),
            ('occurred_at', models.DateTimeField()), ('key_id', models.CharField(default='', max_length=68)),
            ('provider', models.CharField(max_length=128)), ('model', models.CharField(max_length=256)),
            ('alias', models.CharField(max_length=256)), ('failed', models.BooleanField()),
            ('quality', models.CharField(max_length=16)), ('counters', models.JSONField()),
        ], options={'default_permissions': (), 'constraints': [
            models.UniqueConstraint(fields=('source', 'event_id'), name='usage_execution_identity')],
            'indexes': [models.Index(fields=['source', 'occurred_at'], name='usage_event_source_time')]}),
        migrations.CreateModel(name='UsageAggregate', fields=identity()+[source(),
            ('day', models.DateField()), ('key_id', models.CharField(default='', max_length=68)),
            ('provider', models.CharField(max_length=128)), ('model', models.CharField(max_length=256)),
            ('alias', models.CharField(max_length=256)), ('requests', models.PositiveBigIntegerField(default=0)),
            ('failed', models.PositiveBigIntegerField(default=0)), ('partial', models.PositiveBigIntegerField(default=0)),
            ('totals', models.JSONField(default=dict)), ('known', models.JSONField(default=dict)),
        ], options={'default_permissions': (), 'constraints': [
            models.UniqueConstraint(fields=('source', 'day', 'key_id', 'provider', 'model', 'alias'),
                                    name='usage_aggregate_dimensions')],
            'indexes': [models.Index(fields=['day', 'source'], name='usage_aggregate_day_source')]}),
    ]
