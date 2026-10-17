"""#686: программа агента отдельно от identity; RoleBundle-семейство переименовано.

Эпик #623 ещё не раскатан, старых строк вне тестов нет: ссылки заменяются,
а не кастуются (прецедент 0007). AgentProfile — доменная запись программы,
не identity-таблица: стабильный agent Actor живёт в самом Actor.
"""
import django.db.models.deletion
import django.utils.timezone
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('access', '0007_actor_credentials'),
    ]

    operations = [
        # Переименование семейства версий программы агента (#686).
        migrations.RenameModel('RoleBundle', 'AgentDefinitionVersion'),
        migrations.RenameModel('RoleBundleSeal', 'AgentDefinitionSeal'),
        migrations.RenameModel('RoleBundleApproval', 'AgentDefinitionApproval'),
        migrations.RenameModel('RoleCapability', 'DefinitionCapability'),
        migrations.RenameModel('RoleSpawn', 'DefinitionSpawn'),
        migrations.RenameModel('RoleSecretName', 'DefinitionSecretName'),
        migrations.RenameField('AgentDefinitionSeal', 'bundle', 'definition'),
        migrations.RenameField('AgentDefinitionApproval', 'bundle', 'definition'),
        migrations.RenameField('DefinitionCapability', 'bundle', 'definition'),
        migrations.RenameField('DefinitionSpawn', 'bundle', 'definition'),
        migrations.RenameField('DefinitionSecretName', 'bundle', 'definition'),
        migrations.RenameField('CreationIntent', 'bundle', 'definition'),
        # Имена constraints следуют новой номенклатуре; состав не меняется.
        migrations.RemoveConstraint('AgentDefinitionVersion', 'access_bundle_digest'),
        migrations.AddConstraint('AgentDefinitionVersion',
            models.UniqueConstraint(fields=('owner', 'project', 'digest'), name='access_definition_digest')),
        migrations.RemoveConstraint('AgentDefinitionVersion', 'access_bundle_driver'),
        migrations.AddConstraint('AgentDefinitionVersion',
            models.CheckConstraint(condition=models.Q(('driver__in', ('any', 'isolating'))), name='access_definition_driver')),
        migrations.RemoveConstraint('DefinitionCapability', 'access_bundle_capability'),
        migrations.AddConstraint('DefinitionCapability',
            models.UniqueConstraint(fields=('definition', 'scope', 'verb'), name='access_definition_capability')),
        migrations.RemoveConstraint('DefinitionSpawn', 'access_bundle_spawn'),
        migrations.AddConstraint('DefinitionSpawn',
            models.UniqueConstraint(fields=('definition', 'role'), name='access_definition_spawn')),
        migrations.RemoveConstraint('DefinitionSecretName', 'access_bundle_secret'),
        migrations.AddConstraint('DefinitionSecretName',
            models.UniqueConstraint(fields=('definition', 'kind', 'name'), name='access_definition_secret')),
        # Доменная запись программы агента: стабильный Actor ↔ текущая версия.
        migrations.CreateModel(
            name='AgentProfile',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('target_id', models.UUIDField()),
                ('revision', models.PositiveBigIntegerField(default=1)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('agent', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT,
                    related_name='agent_profile', to=settings.AUTH_USER_MODEL)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                    related_name='+', to=settings.AUTH_USER_MODEL)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='catalog.project')),
                ('definition', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='access.agentdefinitionversion')),
            ],
            options={
                'abstract': False,
                'default_permissions': (),
            },
        ),
        migrations.AddConstraint(
            model_name='agentprofile',
            constraint=models.UniqueConstraint(fields=('project', 'target_id'), name='access_agent_profile_target'),
        ),
        migrations.AddConstraint(
            model_name='agentprofile',
            constraint=models.CheckConstraint(condition=models.Q(('revision__gte', 1)), name='access_agent_profile_revision'),
        ),
    ]
