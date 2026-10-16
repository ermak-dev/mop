"""Настоящие права controller до запуска первого API."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('controller', '0002_restore_guard')]
    operations = [migrations.AlterModelOptions(name='installation', options={
        'permissions': [('manage_proxy', 'Can manage proxy'), ('view_models', 'Can view models')]})]
