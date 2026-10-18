"""#707: recovery читает исходную spec регистрации, не новый шаблон."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('operations', '0012_scheduled_node')]
    operations = [
        migrations.AddField(model_name='createbinding', name='dispatched_job', field=models.JSONField(null=True)),
    ]
