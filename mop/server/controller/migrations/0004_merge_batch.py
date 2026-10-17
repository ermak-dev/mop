"""Сохраняем все независимые ветви схемы общего эпика #623."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('controller', '0003_installation_control_permissions'), ('controller', '0003_trust_acl')]
    operations = []
