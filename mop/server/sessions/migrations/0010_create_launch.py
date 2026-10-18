"""#643: имя папета у назначения и пины запуска инкарнации."""
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [('execution', '0009_node_executor')]
    operations = [
        migrations.AddField(model_name='assignment', name='display_name',
                            field=models.CharField(max_length=64, null=True)),
        migrations.AddConstraint(model_name='assignment', constraint=models.UniqueConstraint(
            fields=('project', 'display_name'), name='assignment_project_display_name')),
        migrations.CreateModel(name='IncarnationLaunch', fields=[
            ('incarnation', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, primary_key=True,
                                                 serialize=False, to='execution.executionincarnation')),
            ('bootstrap_revision', models.UUIDField()),
            ('bootstrap_digest', models.CharField(max_length=64)),
            ('image_digest', models.CharField(max_length=64)),
            ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
        ], options={'default_permissions': (), 'constraints': [models.CheckConstraint(
            condition=Q(bootstrap_digest__regex='^[0-9a-f]{64}$', image_digest__regex='^[0-9a-f]{64}$'),
            name='incarnation_launch_pinned_digests')]}),
    ]
