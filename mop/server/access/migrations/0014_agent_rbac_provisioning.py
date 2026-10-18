from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import uuid


class Migration(migrations.Migration):
    dependencies = [('access', '0013_create_grant_activation'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='AgentRBACProfile', fields=[
            ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('group_ids', models.JSONField()), ('digest', models.CharField(max_length=64)),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
            ('definition', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='access.agentdefinitionversion')),
            ('approved_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL)),
        ], options={'abstract': False, 'default_permissions': (),
                    'permissions': [('provision_agent_rbac', 'Can provision approved initial agent RBAC')]}),
        migrations.CreateModel(name='ProfileRevocation', fields=[
            ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
            ('profile', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, to='access.agentrbacprofile')),
            ('revoked_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL)),
        ], options={'abstract': False, 'default_permissions': ()}),
        migrations.CreateModel(name='AgentRBACProvisioning', fields=[
            ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('profile_id', models.UUIDField()), ('profile_digest', models.CharField(max_length=64)),
            ('policy_id', models.UUIDField()), ('policy_digest', models.CharField(max_length=64)),
            ('reservation_id', models.UUIDField()),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
            ('subject', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='rbac_provisioning', to=settings.AUTH_USER_MODEL)),
            ('grant', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, to='access.grant')),
            ('applied_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL)),
        ], options={'abstract': False, 'default_permissions': ()}),
    ]
