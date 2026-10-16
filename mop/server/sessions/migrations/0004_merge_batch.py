"""Сохраняем все независимые ветви схемы общего эпика #623."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('execution', '0002_client_endpoints'), ('execution', '0002_proxy_usage'), ('execution', '0002_session_telemetry'), ('execution', '0002_terminal_control'), ('execution', '0002_unified_read_projection'), ('execution', '0003_assignment_control_permissions')]
    operations = []
