"""#625: подтверждённые stop/cutover замены без связи с Operations ORM."""
import uuid
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('execution', '0012_launch_read_permission')]
    operations = [migrations.CreateModel(name='ReplacementReceipt', fields=[
        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
        ('revision', models.PositiveBigIntegerField(default=1)),
        ('operation_id', models.UUIDField()),
        ('phase', models.CharField(max_length=16)),
        ('step_id', models.UUIDField()),
        ('token', models.UUIDField()),
        ('receipt_id', models.UUIDField(unique=True)),
        ('binding_digest', models.CharField(max_length=64)),
        ('resulting_revision', models.PositiveBigIntegerField()),
        ('resulting_generation', models.PositiveBigIntegerField()),
        ('modify_index', models.PositiveBigIntegerField()),
        ('assignment', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='execution.assignment')),
        ('source_incarnation', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                                to='execution.executionincarnation')),
    ], options={'default_permissions': (), 'constraints': [
        models.CheckConstraint(condition=models.Q(revision__gte=1), name='replacementreceipt_revision_positive'),
        models.UniqueConstraint(fields=('operation_id', 'phase'), name='replacement_operation_phase'),
        models.CheckConstraint(condition=models.Q(phase__in=('stop', 'cutover'), resulting_revision__gte=1,
            resulting_generation__gte=1, modify_index__gte=1), name='replacement_confirmed_transition'),
    ]})]
