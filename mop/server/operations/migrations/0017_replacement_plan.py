from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('operations', '0016_replacement_preparation')]
    operations = [migrations.CreateModel(name='ReplacementPlan', fields=[
        ('operation', models.OneToOneField(primary_key=True, serialize=False,
            on_delete=django.db.models.deletion.PROTECT, to='operations.operation')),
        ('source_executor_epoch', models.UUIDField()),
        ('owner_id', models.PositiveBigIntegerField()),
        ('project_id', models.UUIDField()),
        ('node_id', models.UUIDField()),
        ('role', models.CharField(max_length=64)),
        ('bundle_digest', models.CharField(max_length=64)),
        ('destination_incarnation_id', models.UUIDField()),
        ('destination_grant_id', models.UUIDField()),
        ('destination_revision', models.PositiveBigIntegerField()),
        ('destination_generation', models.PositiveBigIntegerField()),
        ('scheduler_id', models.UUIDField()),
        ('job_id', models.CharField(max_length=128)),
        ('namespace', models.CharField(max_length=128)),
        ('modify_index', models.PositiveBigIntegerField()),
        ('origin', models.CharField(max_length=2048)),
        ('source_ref', models.CharField(max_length=256)),
        ('source_commit', models.CharField(max_length=64)),
        ('clone_ref', models.UUIDField()),
        ('continuation_ref', models.UUIDField(null=True)),
        ('fresh', models.BooleanField()),
        ('source_bootstrap_revision', models.UUIDField()),
        ('source_bootstrap_digest', models.CharField(max_length=64)),
        ('source_image_id', models.UUIDField()),
        ('source_image_digest', models.CharField(max_length=64)),
        ('bootstrap_revision', models.UUIDField()),
        ('bootstrap_digest', models.CharField(max_length=64)),
        ('image', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='catalog.imagerevision')),
        ('image_digest', models.CharField(max_length=64)),
    ], options={'default_permissions': (), 'constraints': [
        models.CheckConstraint(condition=models.Q(destination_revision__gte=1,
            destination_generation__gte=1, modify_index__gte=1), name='replacement_plan_counters'),
        models.CheckConstraint(condition=models.Q(fresh=True, continuation_ref__isnull=True)
            | models.Q(fresh=False, continuation_ref__isnull=False), name='replacement_exact_continuation'),
    ]})]
