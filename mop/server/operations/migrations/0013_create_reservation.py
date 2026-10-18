"""#704: неизменный create intent переживает потерю reserve-ответа до Operation."""
import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('operations', '0012_scheduled_node'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name='CreateReservation', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ('actor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ('request_id', models.UUIDField()),
            ('digest', models.CharField(max_length=64)),
            ('expires_at', models.DateTimeField()),
        ], options={'abstract': False, 'default_permissions': (), 'constraints': [
            models.UniqueConstraint(fields=('actor', 'request_id'), name='create_reservation_actor_request')]}),
    ]
