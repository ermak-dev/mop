"""#643: доверенная привязка узла к планировщику."""
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('operations', '0011_create'), ('catalog', '0001_initial')]
    operations = [
        migrations.CreateModel(name='ScheduledNode', fields=[
            ('node', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, primary_key=True,
                                          serialize=False, to='catalog.node')),
            ('scheduler_id', models.UUIDField()),
            ('nomad_node_id', models.UUIDField()),
            ('capabilities', models.JSONField()),
            ('enrolled_at', models.DateTimeField(default=django.utils.timezone.now)),
        ], options={'default_permissions': ()}),
    ]
