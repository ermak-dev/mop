"""#643: эпоха executor узла, заявленная последним restarted."""
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('execution', '0008_assignment_deleted')]
    operations = [
        migrations.CreateModel(name='NodeExecutor', fields=[
            ('node', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, primary_key=True,
                                          serialize=False, to='catalog.node')),
            ('executor_epoch', models.UUIDField()),
            ('registered_at', models.DateTimeField(default=django.utils.timezone.now)),
        ], options={'default_permissions': ()}),
    ]
