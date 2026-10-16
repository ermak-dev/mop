"""Сведение независимых leaves #626/#656 без изменения их схем и протоколов."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('execution', '0002_proxy_usage'),
        ('execution', '0002_unified_read_projection'),
    ]
    operations = []
