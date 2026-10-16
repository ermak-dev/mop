"""Identity нового хранилища; старые файлы не являются входом миграции."""
import django.utils.timezone
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name='Installation',
            fields=[
                ('id', models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ('identity', models.CharField(max_length=64, unique=True)),
                ('root_digest', models.CharField(editable=False, max_length=64)),
                ('epoch', models.UUIDField(default=uuid.uuid4, editable=False)),
                ('effects_enabled', models.BooleanField(default=False, editable=False)),
                ('initialized_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
            ],
            options={'constraints': [models.CheckConstraint(condition=models.Q(id=1), name='controller_single_installation')]},
        ),
    ]
