"""Независимые producer authority и replacement receipts в одном execution app."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('execution', '0013_current_producer'), ('execution', '0013_replacement_receipt')]
    operations = []
