# Сгенерировано Django 5.2.18; начальная схема нового encrypted core #637.

import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('catalog', '0001_initial'),
        ('controller', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Secret',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('kind', models.CharField(max_length=16)),
                ('name', models.CharField(max_length=256)),
                ('purpose', models.CharField(max_length=32)),
                ('revision', models.PositiveBigIntegerField(default=1)),
                ('last_number', models.PositiveBigIntegerField(default=0)),
                ('revoked', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('created_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ('installation', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='controller.installation')),
                ('project', models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, to='catalog.project')),
            ],
            options={
                'permissions': (('view_secret_metadata', 'View secret metadata'), ('manage_project_secret', 'Manage project secrets'), ('deliver_secret', 'Deliver addressed secrets'), ('rotate_proxy_key', 'Rotate proxy credential')),
                'abstract': False,
                'default_permissions': (),
            },
        ),
        migrations.CreateModel(
            name='CredentialRotation',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('secret_revision', models.PositiveBigIntegerField()),
                ('revision', models.PositiveBigIntegerField(default=0)),
                ('state', models.CharField(choices=[('staged', 'staged'), ('delivering', 'delivering'), ('active', 'active'), ('retiring', 'retiring'), ('retired', 'retired'), ('reconciling', 'reconciling'), ('failed', 'failed'), ('recovery_required', 'recovery_required'), ('restoring_old', 'restoring_old'), ('old_restored', 'old_restored'), ('revoking_new', 'revoking_new'), ('compensated', 'compensated'), ('partial', 'partial')], max_length=32)),
                ('pending', models.CharField(choices=[('deliver', 'deliver'), ('retire_old', 'retire_old'), ('restore_old', 'restore_old'), ('revoke_new', 'revoke_new')], max_length=32, null=True)),
                ('retryable', models.CharField(choices=[('deliver', 'deliver'), ('retire_old', 'retire_old'), ('restore_old', 'restore_old'), ('revoke_new', 'revoke_new')], max_length=32, null=True)),
                ('original_failure', models.CharField(choices=[('deliver', 'deliver'), ('retire_old', 'retire_old'), ('restore_old', 'restore_old'), ('revoke_new', 'revoke_new')], max_length=32, null=True)),
                ('compensation_failure', models.CharField(choices=[('deliver', 'deliver'), ('retire_old', 'retire_old'), ('restore_old', 'restore_old'), ('revoke_new', 'revoke_new')], max_length=32, null=True)),
                ('original_reason', models.CharField(choices=[('refused', 'refused'), ('precondition_failed', 'precondition_failed'), ('unavailable_before_effect', 'unavailable_before_effect'), ('partially_applied', 'partially_applied')], max_length=32, null=True)),
                ('compensation_reason', models.CharField(choices=[('refused', 'refused'), ('precondition_failed', 'precondition_failed'), ('unavailable_before_effect', 'unavailable_before_effect'), ('partially_applied', 'partially_applied')], max_length=32, null=True)),
                ('claim_id', models.UUIDField(null=True)),
                ('generation', models.PositiveBigIntegerField(default=0)),
                ('delivery_generation', models.PositiveBigIntegerField(default=0)),
                ('resolved', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('secret', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='secrets.secret')),
            ],
            options={
                'abstract': False,
                'default_permissions': (),
            },
        ),
        migrations.CreateModel(
            name='SecretBinding',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('controller', models.BooleanField(default=False)),
                ('role', models.CharField(max_length=128)),
                ('digest', models.CharField(max_length=64)),
                ('incarnation', models.UUIDField()),
                ('grant_id', models.UUIDField()),
                ('assignment_id', models.UUIDField()),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('client', models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, to='catalog.clientregistration')),
                ('node', models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, to='catalog.node')),
                ('secret', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='secrets.secret')),
            ],
            options={
                'abstract': False,
                'default_permissions': (),
            },
        ),
        migrations.CreateModel(
            name='RotationRecipient',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('rotation', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='secrets.credentialrotation')),
                ('binding', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='secrets.secretbinding')),
            ],
            options={
                'abstract': False,
                'default_permissions': (),
            },
        ),
        migrations.CreateModel(
            name='SecretVersion',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('number', models.PositiveBigIntegerField()),
                ('envelope', models.BinaryField()),
                ('key_id', models.CharField(editable=False, max_length=64)),
                ('nonce', models.BinaryField()),
                ('size', models.PositiveIntegerField()),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('secret', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='secrets.secret')),
            ],
            options={
                'abstract': False,
                'default_permissions': (),
            },
        ),
        migrations.AddField(
            model_name='secret',
            name='active_version',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='secrets.secretversion'),
        ),
        migrations.CreateModel(
            name='DeliveryReceipt',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('claim_id', models.UUIDField()),
                ('generation', models.PositiveBigIntegerField()),
                ('outcome', models.CharField(choices=[('applied', 'applied'), ('rejected_without_effect', 'rejected_without_effect'), ('unknown', 'unknown')], max_length=32)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('recipient', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='secrets.rotationrecipient')),
                ('version', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='secrets.secretversion')),
            ],
            options={
                'abstract': False,
                'default_permissions': (),
            },
        ),
        migrations.AddField(
            model_name='credentialrotation',
            name='new_version',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='secrets.secretversion'),
        ),
        migrations.AddField(
            model_name='credentialrotation',
            name='old_version',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='secrets.secretversion'),
        ),
        migrations.CreateModel(
            name='SecretAudit',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('action', models.CharField(max_length=24)),
                ('revision', models.PositiveBigIntegerField()),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('actor', models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ('rotation', models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, to='secrets.credentialrotation')),
                ('secret', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='secrets.secret')),
            ],
            options={
                'abstract': False,
                'default_permissions': (),
                'constraints': [models.CheckConstraint(condition=models.Q(('action__in', ('create', 'stage', 'update', 'bind', 'revoke', 'rotation_stage', 'rotation_begin', 'rotation_record', 'rotation_reconcile', 'rotation_activate', 'receipt'))), name='secret_audit_action')],
            },
        ),
        migrations.AddConstraint(
            model_name='secretbinding',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('client__isnull', True), ('controller', False), ('node__isnull', False)), models.Q(('client__isnull', False), ('controller', False), ('node__isnull', True)), models.Q(('client__isnull', True), ('controller', True), ('node__isnull', True)), _connector='OR'), name='secret_binding_one_recipient'),
        ),
        migrations.AddConstraint(
            model_name='rotationrecipient',
            constraint=models.UniqueConstraint(fields=('rotation', 'binding'), name='rotation_recipient_unique'),
        ),
        migrations.AddConstraint(
            model_name='secretversion',
            constraint=models.UniqueConstraint(fields=('secret', 'number'), name='secret_version_number'),
        ),
        migrations.AddConstraint(
            model_name='secretversion',
            constraint=models.UniqueConstraint(fields=('key_id', 'nonce'), name='secret_key_nonce'),
        ),
        migrations.AddConstraint(
            model_name='secretversion',
            constraint=models.CheckConstraint(condition=models.Q(('number__gte', 1)), name='secret_version_positive'),
        ),
        migrations.AddConstraint(
            model_name='secretversion',
            constraint=models.CheckConstraint(condition=models.Q(('size__lte', 1048576)), name='secret_size_bound'),
        ),
        migrations.AddConstraint(
            model_name='secret',
            constraint=models.CheckConstraint(condition=models.Q(('revision__gte', 1)), name='secret_revision_positive'),
        ),
        migrations.AddConstraint(
            model_name='secret',
            constraint=models.CheckConstraint(condition=models.Q(('kind__in', ('variable', 'file', 'credential'))), name='secret_known_kind'),
        ),
        migrations.AddConstraint(
            model_name='secret',
            constraint=models.CheckConstraint(condition=models.Q(('purpose__in', ('project', 'node', 'bootstrap_ssh', 'nomad', 'signer', 'proxy', 'nats', 'ldap_bind', 'tls', 'grant', 'git', 'policy', 'operation'))), name='secret_known_purpose'),
        ),
        migrations.AddConstraint(
            model_name='secret',
            constraint=models.UniqueConstraint(condition=models.Q(('project__isnull', False)), fields=('project', 'kind', 'name'), name='secret_project_name'),
        ),
        migrations.AddConstraint(
            model_name='secret',
            constraint=models.UniqueConstraint(condition=models.Q(('project__isnull', True)), fields=('installation', 'kind', 'name'), name='secret_installation_name'),
        ),
        migrations.AddConstraint(
            model_name='deliveryreceipt',
            constraint=models.UniqueConstraint(fields=('recipient', 'generation'), name='delivery_receipt_identity'),
        ),
        migrations.AddConstraint(
            model_name='deliveryreceipt',
            constraint=models.CheckConstraint(condition=models.Q(('generation__gte', 1)), name='receipt_generation_positive'),
        ),
        migrations.AddConstraint(
            model_name='deliveryreceipt',
            constraint=models.CheckConstraint(condition=models.Q(('outcome__in', ('applied', 'rejected_without_effect', 'unknown'))), name='receipt_known_outcome'),
        ),
        migrations.AddConstraint(
            model_name='credentialrotation',
            constraint=models.UniqueConstraint(condition=models.Q(('resolved', False)), fields=('secret',), name='secret_one_unresolved_rotation'),
        ),
        migrations.AddConstraint(
            model_name='credentialrotation',
            constraint=models.CheckConstraint(condition=models.Q(('secret_revision__gte', 1)), name='rotation_secret_revision'),
        ),
        migrations.AddConstraint(
            model_name='credentialrotation',
            constraint=models.CheckConstraint(condition=models.Q(('state__in', ('staged', 'delivering', 'active', 'retiring', 'retired', 'reconciling', 'failed', 'recovery_required', 'restoring_old', 'old_restored', 'revoking_new', 'compensated', 'partial'))), name='rotation_known_state'),
        ),
        migrations.AddConstraint(
            model_name='credentialrotation',
            constraint=models.CheckConstraint(condition=models.Q(('old_version', models.F('new_version')), _negated=True), name='rotation_distinct_versions'),
        ),
        migrations.AddConstraint(
            model_name='credentialrotation',
            constraint=models.CheckConstraint(condition=models.Q(('resolved', False), ('state__in', ('retired', 'compensated')), _connector='OR'), name='rotation_resolution_terminal'),
        ),
    ]
