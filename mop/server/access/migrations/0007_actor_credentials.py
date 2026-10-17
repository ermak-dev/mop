"""#684: credential привязан к Actor; Principal больше не альтернативная identity.

Новых Principal rows не создаётся; Grant/CreationIntent/GrantEvent ссылаются
на самого Actor. Ссылки заменяются, а не кастуются: эпик #623 ещё не
раскатан, старых строк вне тестов нет.
"""
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('access', '0006_rolebundleseal'),
    ]

    operations = [
        # CredentialBinding теряет дубль атрибутов Actor; остаются только
        # proven fences: generation HMAC, subject-change digest, узел, ссылки.
        migrations.RemoveField(model_name='credentialbinding', name='principal'),
        migrations.RemoveField(model_name='credentialbinding', name='principal_revision'),
        migrations.RemoveField(model_name='credentialbinding', name='principal_kind'),
        # Машина делегирования и намерение создания ссылаются на самого Actor.
        migrations.RemoveField(model_name='grant', name='principal'),
        migrations.AddField(
            model_name='grant',
            name='actor',
            field=models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                related_name='grant_machine'),
        ),
        migrations.RemoveField(model_name='creationintent', name='actor'),
        migrations.AddField(
            model_name='creationintent',
            name='actor',
            field=models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+'),
        ),
        migrations.RemoveField(model_name='grantevent', name='actor'),
        migrations.AddField(
            model_name='grantevent',
            name='actor',
            field=models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name='+'),
        ),
    ]
