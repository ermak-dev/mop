"""Сохраняем все независимые ветви схемы общего эпика #623."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('operations', '0003_image_build'), ('operations', '0004_typed_restart')]
    operations = []
