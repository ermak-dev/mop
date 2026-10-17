"""CONTROL permissions на настоящем execution.Assignment."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('execution', '0002_receipt_constraint_namespace')]
    operations = [migrations.AlterModelOptions(name='assignment', options={'permissions':
        [('assign_work', 'Can assign human work')] + [(code, 'Can '+code.replace('_', ' ')) for code in (
            'create_puppet', 'update_puppet', 'restart_puppet', 'recycle_puppet', 'delete_puppet', 'wipe_puppet',
            'stop_allocation', 'send_message', 'dispatch_task', 'view_delivery', 'register_endpoint', 'deliver_report',
            'view_history', 'view_tail', 'read_terminal', 'control_terminal', 'send_session_command',
            'change_session_model', 'view_landing')]})]
