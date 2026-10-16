"""Объединение опубликованного client leaf с общим графом новой установки."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('execution', '0004_merge_batch'),
        ('execution', '0003_merge_proxy_usage_read_projection'),
    ]
    operations = []
