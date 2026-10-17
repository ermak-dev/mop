from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [('execution', '0006_actor_correlation')]
    operations = [
        migrations.RemoveConstraint(model_name='assignmentattempt', name='attempt_named_action'),
        migrations.AddConstraint(model_name='assignmentattempt', constraint=models.CheckConstraint(
            condition=Q(action__in=('restart_session', 'apply_identity', 'send_message', 'confirm_ready', 'wipe_clone')),
            name='attempt_named_action')),
    ]
