"""Общие operation permissions принадлежат существующему aggregate."""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('operations', '0002_dispatch_checkpoint')]
    operations = [migrations.AlterModelOptions(name='operation', options={'permissions':
        [(code, 'Can '+code.replace('_', ' ')) for code in (
            'cancel_operation', 'recover_operation', 'view_audit', 'stop_puppet_job', 'override_assignment',
            'diagnose_pool', 'treat_pool', 'collect_pool_garbage', 'sweep_pool', 'view_node_disk')]})]
