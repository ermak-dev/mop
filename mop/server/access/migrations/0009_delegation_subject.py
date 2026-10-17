"""#688: делегирование Actor→Actor; Grant-машина без User-per-Grant.

Эпик #623 ещё не раскатан, старых строк вне тестов нет: субъект становится
существующим agent Actor (#686), отдельный service User на выдачу не
создаётся. Поле actor→subject переименованием с OneToOne на FK: один
субъект может держать несколько независимых делегирований.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('access', '0008_agent_profile'),
    ]

    operations = [
        migrations.RemoveField('Grant', 'actor'),
        migrations.AddField('Grant', 'subject',
            models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                related_name='delegations', to=settings.AUTH_USER_MODEL)),
    ]
