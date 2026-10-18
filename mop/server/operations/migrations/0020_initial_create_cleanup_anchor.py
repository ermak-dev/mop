from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [('operations', '0019_initial_create_selection')]
    operations = [
        migrations.AddField(model_name='schedulednode', name='anchor_executable', field=models.CharField(max_length=512, blank=True)),
        migrations.AddField(model_name='schedulednode', name='anchor_digest', field=models.CharField(max_length=64, blank=True)),
        migrations.CreateModel(name='CreateCleanup', fields=[
            ('operation', models.OneToOneField(to='operations.operation', primary_key=True, serialize=False, on_delete=django.db.models.deletion.PROTECT)),
            ('armed_at', models.DateTimeField(default=django.utils.timezone.now)),
            ('required_at', models.DateTimeField(null=True)), ('confirmed_at', models.DateTimeField(null=True)),
            ('adopted_at', models.DateTimeField(null=True)),
        ], options={'default_permissions': ()}),
    ]
