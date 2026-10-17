"""#694: учёт неудачных входов ведёт django-axes; самописная таблица уходит.

Эпик #623 ещё не раскатан: строк вне тестов нет, переносить нечего.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('access', '0010_merge_delegation')]
    operations = [migrations.DeleteModel(name='AuthAttempt')]
