"""#684: correlation ссылается на Actor id, не на параллельный Principal UUID.

Дубли (user id рядом на той же строке) удалены; исполнители хранят int id
Actor. Ссылки заменяются, а не кастуются: эпик #623 ещё не раскатан, старых
строк вне тестов нет.
"""
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ('execution', '0005_merge_client_batch'),
    ]

    operations = [
        # Сначала уникальность со старыми полями, потом сами поля: PG сам
        # дропает index при удалении колонки, Django state должен идти раньше.
        migrations.RemoveConstraint(model_name='assignmentattempt', name='attempt_request_identity'),
        migrations.RemoveField(model_name='assignmentattempt', name='principal_id'),
        migrations.AddField(model_name='assignmentattempt', name='executor_user_id',
            field=models.PositiveBigIntegerField()),
        migrations.AddConstraint(model_name='assignmentattempt',
            constraint=models.UniqueConstraint(fields=('executor_user_id', 'request_id'), name='attempt_request_identity')),
        migrations.RemoveField(model_name='connectionlease', name='principal_id'),
        migrations.AddField(model_name='connectionlease', name='user_id',
            field=models.PositiveBigIntegerField()),
        migrations.RemoveConstraint(model_name='messagedelivery', name='endpoint_message_identity'),
        migrations.RemoveField(model_name='messagedelivery', name='sender_principal_id'),
        migrations.AddConstraint(model_name='messagedelivery',
            constraint=models.UniqueConstraint(fields=('sender_user_id', 'message_id'), name='endpoint_message_identity')),
        migrations.RemoveField(model_name='turnoutcome', name='initiator_principal_id'),
        migrations.RemoveConstraint(model_name='privatehistorycursor', name='history_private_cursor_scope'),
        migrations.RemoveField(model_name='privatehistorycursor', name='principal_id'),
        migrations.AddConstraint(model_name='privatehistorycursor',
            constraint=models.UniqueConstraint(fields=('user', 'observation'), name='history_private_cursor_scope')),
        migrations.RemoveField(model_name='terminalattachment', name='principal_id'),
        migrations.AddField(model_name='terminalattachment', name='user_id',
            field=models.PositiveBigIntegerField()),
    ]
