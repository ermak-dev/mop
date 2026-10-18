"""#709: общий admission учитывает provisioning до появления Operation."""
import uuid

from django.db import migrations, models
import django.db.models.deletion


def retain_unresolved(apps, schema_editor):
    # Старый reservation мог пережить COMMIT внешнего эффекта. Нельзя считать
    # отсутствие новой попытки доказанным отказом и молча освобождать его слот.
    reservations = apps.get_model('operations', 'CreateReservation')
    attempts = apps.get_model('operations', 'CreateProvisioningAttempt')
    alias = schema_editor.connection.alias
    for reservation in reservations.objects.using(alias).iterator():
        attempts.objects.using(alias).create(reservation_id=reservation.pk)


class Migration(migrations.Migration):
    dependencies = [('operations', '0014_merge_create_recovery')]
    operations = [
        migrations.AddField(model_name='createreservation', name='epoch', field=models.UUIDField(null=True)),
        migrations.AddField(model_name='createreservation', name='project', field=models.ForeignKey(
            null=True, on_delete=django.db.models.deletion.PROTECT, to='catalog.project')),
        migrations.CreateModel(name='CreateProvisioningAttempt', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ('refused_at', models.DateTimeField(null=True)),
            ('reservation', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                             to='operations.createreservation')),
        ], options={'default_permissions': ()}),
        migrations.RunPython(retain_unresolved, migrations.RunPython.noop),
    ]
