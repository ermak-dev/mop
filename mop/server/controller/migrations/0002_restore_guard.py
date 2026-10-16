"""Guard создаётся только штатной миграцией и не входит в backup."""
import uuid
from django.db import migrations, models


def provision_guard(apps, schema_editor):
    # Даже при уже существующей Installation миграция не разрешает startup.
    apps.get_model('controller', 'RestoreGuard').objects.using(schema_editor.connection.alias).create(pk=1)


class Migration(migrations.Migration):
    dependencies = [('controller', '0001_initial')]
    operations = [
        migrations.CreateModel(
            name='RestoreGuard',
            fields=[
                ('id', models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ('generation', models.UUIDField(default=uuid.uuid4, editable=False)),
                ('state', models.CharField(choices=[('uninitialized', 'Uninitialized'), ('ready', 'Ready'),
                                                    ('restoring', 'Restoring')],
                                          default='uninitialized', editable=False, max_length=16)),
            ],
            options={'constraints': [
                models.CheckConstraint(condition=models.Q(id=1), name='controller_single_restore_guard'),
                models.CheckConstraint(condition=models.Q(state__in=['uninitialized', 'ready', 'restoring']),
                                       name='controller_restore_guard_state'),
            ]},
        ),
        migrations.RunPython(provision_guard, migrations.RunPython.noop),
    ]
