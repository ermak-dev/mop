"""Dispatch и fresh physical skip evidence не исчезают при разрыве Unix client."""
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('builds','0001_initial')]
    operations = [
        migrations.AddField(model_name='buildrun', name='skipped', field=models.BooleanField(default=False)),
        migrations.AlterModelOptions(name='buildrun', options={'default_permissions': (), 'permissions': [('force_build','Can force image build safety')]}),
        migrations.CreateModel(name='ExecutorDispatch', fields=[
            ('id',models.BigAutoField(primary_key=True,serialize=False,auto_created=True,verbose_name='ID')),
            ('intent',models.OneToOneField('builds.stageintent',on_delete=models.PROTECT)),
            ('created_at',models.DateTimeField(default=django.utils.timezone.now)),
        ], options={'default_permissions': ()}),
        migrations.CreateModel(name='AvailabilityReceipt', fields=[
            ('id',models.BigAutoField(primary_key=True,serialize=False,auto_created=True,verbose_name='ID')),
            ('build',models.ForeignKey('builds.buildrun',on_delete=models.PROTECT)),
            ('node',models.ForeignKey('catalog.node',on_delete=models.PROTECT)),
            ('source_digest',models.CharField(max_length=64)),
            ('external_reference',models.CharField(max_length=512)),
            ('observed_at',models.DateTimeField()),
            ('received_at',models.DateTimeField(default=django.utils.timezone.now)),
        ], options={'default_permissions': (), 'constraints':[models.UniqueConstraint(fields=('build','node'),name='build_availability_node')]}),
    ]
