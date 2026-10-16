"""Seal создаёт только проверенная publication, прежние bundles не получают доверия."""
import uuid

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('access', '0005_creationintent_actor_credential_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='RoleBundleSeal',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('bundle', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, to='access.rolebundle')),
            ],
            options={
                'abstract': False,
                'default_permissions': (),
            },
        ),
    ]
