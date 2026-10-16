from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('operations','0006_remove_operation_operation_finite_action_and_more')]
    operations = [
        migrations.RemoveConstraint(model_name='operation', name='operation_finite_action'),
        migrations.RemoveConstraint(model_name='operation', name='operation_typed_target'),
        migrations.RemoveConstraint(model_name='step', name='step_finite_handler'),
        migrations.AddConstraint(model_name='operation', constraint=models.CheckConstraint(
            condition=models.Q(action__in=('drain','enable','restart','build','stop_job')), name='operation_finite_action')),
        migrations.AddConstraint(model_name='operation', constraint=models.CheckConstraint(
            condition=(models.Q(target_kind='node',action__in=('drain','enable','build'),eligible__isnull=False,
                                target_id__isnull=True,incarnation_id__isnull=True) |
                       models.Q(target_kind='puppet',action__in=('restart','stop_job'),eligible__isnull=True,
                                target_id__isnull=False,incarnation_id__isnull=False)), name='operation_typed_target')),
        migrations.AddConstraint(model_name='step', constraint=models.CheckConstraint(condition=models.Q(
            name__in=('node_eligibility','apply_identity','restart_session','confirm_ready','image_build','scheduler_stop')),
            name='step_finite_handler')),
        migrations.CreateModel(name='ScheduledPuppet', fields=[
            ('target_id', models.UUIDField(primary_key=True,serialize=False)),
            ('incarnation_id',models.UUIDField()), ('epoch',models.UUIDField()), ('restore_generation',models.UUIDField()),
            ('scheduler_id',models.UUIDField()), ('job_id',models.CharField(max_length=128)),
            ('namespace',models.CharField(max_length=128)), ('modify_index',models.PositiveBigIntegerField()),
        ], options={'default_permissions':(), 'constraints':[
            models.UniqueConstraint(fields=('scheduler_id','namespace','job_id'),name='scheduled_job_identity_unique'),
            models.CheckConstraint(condition=models.Q(modify_index__gte=1),name='scheduled_job_positive_index')]}),
        migrations.CreateModel(name='StopBinding', fields=[
            ('applied_revision',models.PositiveBigIntegerField(null=True)),
            ('operation',models.OneToOneField(primary_key=True,on_delete=django.db.models.deletion.PROTECT,serialize=False,to='operations.operation')),
            ('project',models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,to='catalog.project')),
            ('executor_epoch',models.UUIDField()), ('owner_id',models.PositiveBigIntegerField()),
            ('generation',models.PositiveBigIntegerField()), ('role',models.CharField(max_length=64)),
            ('bundle_digest',models.CharField(max_length=64)), ('grant_id',models.UUIDField()),
            ('scheduler_id',models.UUIDField()), ('job_id',models.CharField(max_length=128)),
            ('namespace',models.CharField(max_length=128)), ('modify_index',models.PositiveBigIntegerField()),
        ],options={'default_permissions':()}),
    ]
