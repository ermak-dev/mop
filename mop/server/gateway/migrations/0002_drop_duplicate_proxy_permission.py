"""#689: право управления прокси одно -- controller.manage_proxy (#685).

Дубль gateway.manage_proxy уходит из Meta; строки Permission удаляются,
чтобы кодовое имя имело одного владельца. Эпик #623 не раскатан: выданных
строк вне тестов нет.
"""
from django.db import migrations


def drop_duplicate(apps, schema_editor):
    Permission = apps.get_model('auth', 'Permission')
    Permission.objects.filter(content_type__app_label='gateway', codename='manage_proxy').delete()


class Migration(migrations.Migration):
    dependencies = [('gateway', '0001_initial'), ('auth', '0012_alter_user_first_name_max_length')]
    operations = [
        migrations.AlterModelOptions(name='circuit', options={'default_permissions': ()}),
        migrations.RunPython(drop_duplicate, migrations.RunPython.noop),
    ]
