from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('secrets', '0002_credentialrotation_rotation_pending_enum_and_more')]
    operations = [
        migrations.RemoveConstraint(model_name='secretaudit', name='secret_audit_action'),
        migrations.AddConstraint(model_name='secretaudit', constraint=models.CheckConstraint(
            condition=models.Q(action__in=('create', 'stage', 'update', 'bind', 'revoke',
                'rotation_stage', 'rotation_begin', 'rotation_record', 'rotation_reconcile',
                'rotation_activate', 'receipt', 'proxy_issue')), name='secret_audit_action')),
    ]
