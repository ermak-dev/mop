"""Намерение сохраняет точную capability создателя, не произвольный login."""
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('access', '0003_creationintent_grant_creationintent_parent_and_more'),
    ]

    operations = [
        # Нет переноса старых capabilities: неизвестный credential закрывает
        # прежнее намерение, а не выбирает случайный действующий credential.
        migrations.AddField(
            model_name='creationintent',
            name='actor_credential_id',
            field=models.UUIDField(default=uuid.UUID(int=0)),
            preserve_default=False,
        ),
    ]
