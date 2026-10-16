"""Конечная команда сборки использует прежние Operation, Step и claims."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('operations', '0002_dispatch_checkpoint')]
    operations = [
        migrations.RemoveConstraint(model_name='operation', name='operation_finite_action'),
        migrations.AddConstraint(model_name='operation', constraint=models.CheckConstraint(
            condition=models.Q(action__in=('drain', 'enable', 'build')), name='operation_finite_action')),
        migrations.RemoveConstraint(model_name='step', name='step_finite_handler'),
        migrations.AddConstraint(model_name='step', constraint=models.CheckConstraint(
            condition=models.Q(name__in=('node_eligibility', 'image_build')), name='step_finite_handler')),
    ]
