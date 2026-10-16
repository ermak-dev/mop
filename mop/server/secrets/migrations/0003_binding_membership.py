from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('secrets', '0002_credentialrotation_rotation_pending_enum_and_more')]

    operations = [
        migrations.AddField(model_name='secretbinding', name='retired_at',
                            field=models.DateTimeField(editable=False, null=True)),
        # Не угадываем, какая из прежних дубликатных привязок всё ещё current:
        # при неоднозначности constraint останавливает migration для явного решения.
        migrations.AddConstraint(model_name='secretbinding', constraint=models.UniqueConstraint(
            fields=('secret', 'node', 'role'), condition=models.Q(retired_at__isnull=True, node__isnull=False),
            name='secret_current_node_role')),
        migrations.AddConstraint(model_name='secretbinding', constraint=models.UniqueConstraint(
            fields=('secret', 'client', 'role'), condition=models.Q(retired_at__isnull=True, client__isnull=False),
            name='secret_current_client_role')),
        migrations.AddConstraint(model_name='secretbinding', constraint=models.UniqueConstraint(
            fields=('secret', 'role'), condition=models.Q(retired_at__isnull=True, controller=True),
            name='secret_current_controller_role')),
        migrations.RemoveConstraint(model_name='secretaudit', name='secret_audit_action'),
        migrations.AddConstraint(model_name='secretaudit', constraint=models.CheckConstraint(
            condition=models.Q(action__in=('create', 'stage', 'update', 'bind', 'retire_binding', 'revoke',
                'rotation_stage', 'rotation_begin', 'rotation_record', 'rotation_reconcile',
                'rotation_activate', 'receipt')), name='secret_audit_action')),
    ]
