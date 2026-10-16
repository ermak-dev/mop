"""Начальный каталог пустой установки; без чтения прежних реестров."""
import uuid
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def identity():
    return [('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('revision', models.PositiveBigIntegerField(default=1))]


def fk(target, **kwargs):
    return models.ForeignKey(to=target, on_delete=django.db.models.deletion.PROTECT, **kwargs)


def options(name, *constraints):
    return {'abstract': False, 'constraints': [models.CheckConstraint(
        condition=models.Q(revision__gte=1), name=name.lower() + '_revision_positive'), *constraints]}


def setting_fields():
    return [('key', models.CharField(max_length=64)), ('value_kind', models.CharField(max_length=8)),
            ('text_value', models.TextField(null=True)), ('int_value', models.BigIntegerField(null=True)),
            ('bool_value', models.BooleanField(null=True))]


def typed(node):
    # Замороженная схема этой миграции, не импорт runtime definitions.
    definitions = [('effect_limit', 'int', False, 1, 8, ()),
                   ('queue_limit', 'int', False, 1, 256, ()),
                   ('node_effect_limit', 'int', True, 1, 4, ()),
                   ('log_level', 'text', True, 0, 0, ('debug', 'info', 'warning', 'error')),
                   ('telemetry_enabled', 'bool', True, 0, 0, ())]
    valid = models.Q(pk__isnull=True)
    for key, kind, override, low, high, choices in definitions:
        if node and not override:
            continue
        clause = models.Q(key=key, value_kind=kind)
        for column in ('text', 'int', 'bool'):
            clause &= models.Q(**{column + '_value__isnull': column != kind})
        if kind == 'int':
            clause &= models.Q(int_value__gte=low, int_value__lte=high)
        if choices:
            clause &= models.Q(**{kind + '_value__in': choices})
        valid |= clause
    return valid


class Migration(migrations.Migration):
    initial = True
    dependencies = [('controller', '0001_initial')]
    operations = [
        migrations.CreateModel(name='Project', fields=identity() + [
            ('origin', models.CharField(max_length=2048, unique=True)),
            ('name', models.CharField(max_length=128, unique=True)),
            ('puppet_limit', models.BigIntegerField(null=True)),
        ], options=options('project', models.CheckConstraint(condition=models.Q(puppet_limit__isnull=True) | models.Q(puppet_limit__gte=0), name='project_limit_nonnegative'))),
        migrations.CreateModel(name='Node', fields=identity() + [
            ('name', models.CharField(max_length=128, unique=True)),
            ('driver', models.CharField(max_length=16)), ('ssh_host', models.CharField(max_length=253)),
            ('ssh_port', models.PositiveIntegerField(default=22)),
            ('control_origin', models.URLField(max_length=2048)), ('lan_address', models.CharField(max_length=253)),
            ('bootstrap_address', models.CharField(max_length=253)),
            ('deployment_member', models.BooleanField(default=False)), ('desired_eligible', models.BooleanField(default=True)),
            ('applied_revision', models.PositiveBigIntegerField(null=True)),
            ('applied_receipt', models.CharField(blank=True, max_length=512)), ('applied_at', models.DateTimeField(null=True)),
            ('observed_eligible', models.BooleanField(null=True)), ('observed_source', models.CharField(blank=True, max_length=256)),
            ('observed_at', models.DateTimeField(null=True)), ('received_at', models.DateTimeField(null=True)),
            ('settings_revision', models.PositiveBigIntegerField(default=1)),
            ('settings_applied_revision', models.PositiveBigIntegerField(null=True)),
            ('settings_receipt', models.CharField(blank=True, max_length=512)), ('settings_applied_at', models.DateTimeField(null=True)),
        ], options=options('node',
            models.CheckConstraint(condition=models.Q(driver__in=('host', 'pve')), name='node_known_driver'),
            models.CheckConstraint(condition=models.Q(ssh_port__gte=1, ssh_port__lte=65535), name='node_ssh_port'),
            models.CheckConstraint(condition=models.Q(settings_revision__gte=1), name='node_settings_revision'),
            models.CheckConstraint(condition=models.Q(applied_revision__isnull=True) | models.Q(applied_revision__lte=models.F('revision')), name='node_application_not_future'))),
        migrations.CreateModel(name='NodeProject', fields=identity() + [
            ('node', fk('catalog.node')), ('project', fk('catalog.project')),
        ], options=options('nodeproject', models.UniqueConstraint(fields=('node', 'project'), name='node_project_unique'))),
        migrations.CreateModel(name='InstallationConfiguration', fields=identity() + [
            ('installation', models.OneToOneField(to='controller.installation', on_delete=django.db.models.deletion.PROTECT)),
            ('applied_revision', models.PositiveBigIntegerField(null=True)),
            ('applied_receipt', models.CharField(blank=True, max_length=512)), ('applied_at', models.DateTimeField(null=True)),
        ], options=options('installationconfiguration')),
        migrations.CreateModel(name='InstallationSetting', fields=identity() + setting_fields() + [
            ('installation', fk('controller.installation')),
        ], options=options('installationsetting',
            models.UniqueConstraint(fields=('installation', 'key'), name='installation_setting_unique'),
            models.CheckConstraint(condition=typed(False), name='installation_setting_typed'))),
        migrations.CreateModel(name='NodeSetting', fields=identity() + setting_fields() + [
            ('node', fk('catalog.node')),
        ], options=options('nodesetting',
            models.UniqueConstraint(fields=('node', 'key'), name='node_setting_unique'),
            models.CheckConstraint(condition=typed(True), name='node_setting_typed'))),
        migrations.CreateModel(name='ProjectPolicySnapshot', fields=identity() + [
            ('project', fk('catalog.project')), ('sequence', models.PositiveBigIntegerField()),
            ('content', models.BinaryField()), ('digest', models.CharField(max_length=64)),
            ('source_commit', models.CharField(max_length=64)), ('source_ref', models.CharField(max_length=256)),
            ('provenance', models.CharField(max_length=256)), ('confidential', models.BooleanField(default=False)),
            ('protection_reference', models.CharField(blank=True, max_length=256)),
            ('cpu', models.PositiveIntegerField()), ('memory_mb', models.PositiveIntegerField()), ('disk_mb', models.PositiveIntegerField()),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
        ], options=options('projectpolicysnapshot',
            models.UniqueConstraint(fields=('project', 'sequence'), name='policy_project_sequence'),
            models.CheckConstraint(condition=models.Q(sequence__gte=1, cpu__gte=1, memory_mb__gte=1, disk_mb__gte=1), name='policy_positive_resources'),
            models.CheckConstraint(condition=models.Q(confidential=False, protection_reference='') | (models.Q(confidential=True) & ~models.Q(protection_reference='')), name='policy_protection_reference'))),
        migrations.CreateModel(name='ImageRevision', fields=identity() + [
            ('policy', fk('catalog.projectpolicysnapshot')), ('source', models.CharField(max_length=256)),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
        ], options=options('imagerevision')),
        migrations.CreateModel(name='NodeImage', fields=identity() + [
            ('node', fk('catalog.node')), ('project', fk('catalog.project')),
            ('desired_image', fk('catalog.imagerevision', null=True, related_name='+')),
            ('observed_image', fk('catalog.imagerevision', null=True, related_name='+')),
            ('available', models.BooleanField(null=True)), ('source', models.CharField(blank=True, max_length=256)),
            ('observed_at', models.DateTimeField(null=True)), ('received_at', models.DateTimeField(null=True)),
            ('receipt', models.CharField(blank=True, max_length=512)),
        ], options=options('nodeimage', models.UniqueConstraint(fields=('node', 'project'), name='node_image_unique'))),
        migrations.CreateModel(name='ClientRegistration', fields=identity() + [
            ('installation', fk('controller.installation')), ('label', models.CharField(max_length=128)),
            ('registered_at', models.DateTimeField(default=django.utils.timezone.now)),
        ], options=options('clientregistration')),
        migrations.CreateModel(name='WorkspaceBinding', fields=identity() + [
            ('client', fk('catalog.clientregistration')), ('project', fk('catalog.project')),
            ('workspace_key', models.CharField(max_length=256)), ('integration_branch', models.CharField(max_length=256)),
        ], options=options('workspacebinding', models.UniqueConstraint(fields=('client', 'workspace_key'), name='workspace_client_key_unique'))),
    ]
