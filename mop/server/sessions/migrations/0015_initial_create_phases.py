from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('execution', '0014_merge_producer_replacement')]
    operations = [
        migrations.RemoveConstraint(model_name='assignmentattempt', name='attempt_named_action'),
        migrations.AddConstraint(model_name='assignmentattempt', constraint=models.CheckConstraint(
            condition=models.Q(action__in=('restart_session', 'apply_identity', 'send_message',
                'confirm_ready', 'wipe_clone', 'prepare_body', 'bootstrap_body', 'initial_identity', 'native_exec')),
            name='attempt_named_action')),
    ]
