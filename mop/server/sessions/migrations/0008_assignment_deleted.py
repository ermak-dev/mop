from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [('execution', '0007_wipe_clone_attempt')]
    operations = [
        migrations.AddField(model_name='assignment', name='deleted_at', field=models.DateTimeField(null=True)),
        migrations.AddConstraint(model_name='assignment', constraint=models.CheckConstraint(
            condition=Q(deleted_at__isnull=True) | Q(owner__isnull=True, current_incarnation__isnull=True),
            name='assignment_deleted_is_closed')),
    ]
