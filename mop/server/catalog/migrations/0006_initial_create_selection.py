import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [('catalog', '0005_alter_node_options_alter_project_options'),
                    migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='InitialCreatePolicy', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ('revision', models.PositiveBigIntegerField(default=1)),
            ('definition_id', models.UUIDField()), ('bootstrap_revision', models.UUIDField()),
            ('source_origin', models.CharField(max_length=2048)),
            ('source_commit', models.CharField(max_length=64)), ('source_ref', models.CharField(max_length=256)),
            ('desired_model', models.CharField(max_length=128)), ('model_config_digest', models.CharField(max_length=64)),
            ('digest', models.CharField(max_length=64)), ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
            ('project', models.ForeignKey(to='catalog.project', on_delete=django.db.models.deletion.PROTECT)),
            ('owner', models.ForeignKey(to=settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.PROTECT)),
            ('image', models.ForeignKey(to='catalog.imagerevision', on_delete=django.db.models.deletion.PROTECT)),
        ], options={'constraints': [
            models.CheckConstraint(condition=models.Q(revision__gte=1), name='initialcreatepolicy_revision_positive'),
            models.CheckConstraint(condition=models.Q(source_commit__regex='^([0-9a-f]{40}|[0-9a-f]{64})$',
                model_config_digest__regex='^[0-9a-f]{64}$', digest__regex='^[0-9a-f]{64}$'), name='initial_create_policy_pins')]}),
        migrations.CreateModel(name='InitialCreateConsumer', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ('revision', models.PositiveBigIntegerField(default=1)), ('purpose', models.CharField(max_length=32)),
            ('version_id', models.UUIDField()),
            ('policy', models.ForeignKey(to='catalog.initialcreatepolicy', on_delete=django.db.models.deletion.PROTECT)),
        ], options={'constraints': [
            models.CheckConstraint(condition=models.Q(revision__gte=1), name='initialcreateconsumer_revision_positive'),
            models.UniqueConstraint(fields=('policy', 'version_id'), name='initial_create_consumer_reference'),
            models.CheckConstraint(condition=models.Q(purpose__in=('source_credential', 'bootstrap_input',
                'model_config', 'model_credential')), name='initial_create_consumer_purpose')]}),
        migrations.CreateModel(name='InitialCreateSelection', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ('revision', models.PositiveBigIntegerField(default=1)), ('definition_id', models.UUIDField()),
            ('project', models.ForeignKey(to='catalog.project', on_delete=django.db.models.deletion.PROTECT)),
            ('owner', models.ForeignKey(to=settings.AUTH_USER_MODEL, on_delete=django.db.models.deletion.PROTECT)),
            ('policy', models.ForeignKey(to='catalog.initialcreatepolicy', on_delete=django.db.models.deletion.PROTECT)),
        ], options={'constraints': [
            models.CheckConstraint(condition=models.Q(revision__gte=1), name='initialcreateselection_revision_positive'),
            models.UniqueConstraint(fields=('project', 'owner', 'definition_id'), name='initial_create_explicit_selection')]}),
    ]
