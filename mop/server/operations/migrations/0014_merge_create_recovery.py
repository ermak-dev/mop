"""Объединение независимых пинов request intent (#704) и dispatch spec (#707)."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('operations', '0013_create_reservation'), ('operations', '0013_registration_spec')]
    operations = []
