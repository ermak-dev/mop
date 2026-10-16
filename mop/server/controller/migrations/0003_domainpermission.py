"""Стандартные Django permissions, без ручных production Permission rows."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('controller', '0002_restore_guard')]
    operations = [migrations.CreateModel(
        name='DomainPermission',
        fields=[('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID'))],
        options={'managed': False, 'default_permissions': (),
                 'permissions': [('manage_proxy', 'Can manage external proxy'), ('view_models', 'Can view model catalogue')]},
    )]
