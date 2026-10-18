from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('catalog', '0006_initial_create_selection')]
    # Старые immutable policies не получают неявный passive/default профиль:
    # до явного нового configure они откажут перед credential I/O (#747).
    operations = [
        migrations.AddField(model_name='initialcreatepolicy', name='rbac_profile_id', field=models.UUIDField(null=True)),
        migrations.AddField(model_name='initialcreatepolicy', name='rbac_profile_digest', field=models.CharField(default='', max_length=64)),
    ]
