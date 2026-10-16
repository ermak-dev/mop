"""ACL trust-таблиц — часть явного schema provisioning, не runtime repair."""
from django.db import migrations


def seal_trust(apps, schema_editor):
    # Default CRUD нужен обычным таблицам, но не authority. Исключение фиксируется
    # до публикации готовой схемы; default ACL не меняет существующие таблицы.
    from mop.server.install_postgresql import seal_trust_tables
    seal_trust_tables(schema_editor.connection.connection)


class Migration(migrations.Migration):
    dependencies = [('controller', '0002_restore_guard')]
    operations = [migrations.RunPython(seal_trust, migrations.RunPython.noop)]
