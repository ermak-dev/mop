from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('operations', '0018_initial_create_phases')]
    operations = [
        migrations.AddField(model_name='createreservation', name='policy_id', field=models.UUIDField(null=True)),
        migrations.AddField(model_name='createreservation', name='policy_digest', field=models.CharField(max_length=64, blank=True)),
        migrations.AddField(model_name='createreservation', name='git_name', field=models.CharField(max_length=256, blank=True)),
        migrations.AddField(model_name='createreservation', name='git_email', field=models.CharField(max_length=256, blank=True)),
        migrations.AddField(model_name='createbinding', name='reservation', field=models.ForeignKey(
            to='operations.createreservation', null=True, on_delete=django.db.models.deletion.PROTECT)),
    ]
