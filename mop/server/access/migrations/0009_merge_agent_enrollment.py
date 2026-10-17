"""Сведение параллельных листов access #686/#687 без изменения их схем."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('access', '0008_agent_profile'),
        ('access', '0008_service_enrollment'),
    ]
    operations = []
