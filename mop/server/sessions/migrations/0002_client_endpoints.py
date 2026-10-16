"""Различаем имя constraint после объединения frozen execution/secrets apps."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('execution', '0001_initial')]
    operations = [
        migrations.RemoveConstraint(model_name='resultreceipt', name='receipt_known_outcome'),
        migrations.AddConstraint(model_name='resultreceipt', constraint=models.CheckConstraint(
            condition=models.Q(outcome__in=('succeeded', 'failed', 'unknown')),
            name='execution_receipt_known_outcome')),
    ]
