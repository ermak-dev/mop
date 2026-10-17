"""Сведение параллельных листов access #688/#686-#687 без изменения их схем."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('access', '0009_delegation_subject'),
        ('access', '0009_merge_agent_enrollment'),
    ]
    operations = []
