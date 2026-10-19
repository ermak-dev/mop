from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('operations', '0017_replacement_plan')]
    operations = [
        migrations.RemoveConstraint(model_name='step', name='step_finite_handler'),
        migrations.AddConstraint(model_name='step', constraint=models.CheckConstraint(
            condition=models.Q(name__in=('node_eligibility', 'apply_identity', 'restart_session',
                'confirm_ready', 'image_build', 'scheduler_stop', 'send_message', 'wipe_clone',
                'scheduler_delete', 'scheduler_register', 'activate_grant', 'prepare_launch',
                'prepare_body', 'bootstrap_body', 'initial_identity', 'native_exec')),
            name='step_finite_handler')),
    ]
