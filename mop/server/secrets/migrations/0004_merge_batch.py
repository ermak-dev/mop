"""Сохраняем все независимые ветви схемы общего эпика #623."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('secrets', '0003_binding_membership'), ('secrets', '0003_proxy_issuance_audit')]
    operations = []
