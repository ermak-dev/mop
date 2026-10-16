"""Retained identity/history и переносимая active uniqueness #634."""
import uuid
from django.db import migrations, models


ZERO = uuid.UUID(int=0)


def retirement_constraint(name):
    # Замороженный инвариант, без импорта runtime models/policy.
    return models.CheckConstraint(
        condition=models.Q(retired_at__isnull=True, identity_slot=ZERO)
            | (models.Q(retired_at__isnull=False, identity_slot=models.F('id')) & ~models.Q(identity_slot=ZERO)),
        name=name + '_retirement_state')


class Migration(migrations.Migration):
    dependencies = [('catalog', '0003_runtime_setting_coverage')]
    operations = [
        migrations.AddField(model_name='project', name='retired_at', field=models.DateTimeField(null=True)),
        migrations.AddField(model_name='project', name='identity_slot', field=models.UUIDField(default=ZERO, editable=False)),
        migrations.AddField(model_name='node', name='retired_at', field=models.DateTimeField(null=True)),
        migrations.AddField(model_name='node', name='identity_slot', field=models.UUIDField(default=ZERO, editable=False)),
        migrations.AlterField(model_name='project', name='origin', field=models.CharField(max_length=2048)),
        migrations.AlterField(model_name='project', name='name', field=models.CharField(max_length=128)),
        migrations.AlterField(model_name='node', name='name', field=models.CharField(max_length=128)),
        migrations.AddConstraint(model_name='project', constraint=retirement_constraint('project')),
        migrations.AddConstraint(model_name='node', constraint=retirement_constraint('node')),
        migrations.AddConstraint(model_name='project', constraint=models.UniqueConstraint(
            fields=('origin', 'identity_slot'), name='project_origin_slot_unique')),
        migrations.AddConstraint(model_name='project', constraint=models.UniqueConstraint(
            fields=('name', 'identity_slot'), name='project_name_slot_unique')),
        migrations.AddConstraint(model_name='node', constraint=models.UniqueConstraint(
            fields=('name', 'identity_slot'), name='node_name_slot_unique')),
        migrations.AddConstraint(model_name='node', constraint=models.CheckConstraint(
            condition=models.Q(retired_at__isnull=True) | models.Q(deployment_member=False), name='retired_node_not_member')),
    ]
