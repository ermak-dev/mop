"""#645: параллельная identity удаляется -- Actor единственный субъект (#683).

Строк не создаётся с #684, права с #685 у Grant, потребители ушли с #688.
Эпик #623 не раскатан: данных вне тестов нет.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('access', '0011_remove_authattempt')]
    operations = [migrations.DeleteModel(name='Principal')]
