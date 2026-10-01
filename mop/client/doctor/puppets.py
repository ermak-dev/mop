"""puppets: stuck sessions, a stale login, restart backoff, model quota, an old job spec

Диагноз и лечение живут в mop/common/puppets.py. Протухший логин (#357)
лечится побудкой без рестарта (#290): разговор переживает отказ API, а
свежие креды папету больше не нужны -- ключ LLM лежит в secrets.env узла
и раздаётся сервисом (#391), логины провайдеров умерли вместе с арендами
(#384). Локального файла машина запуска doctor не читает -- расписанный
с сервера doctor (#359) его и не имеет.
"""
from mop.common import puppets


def diagnose():
    """Проблемы puppets.diagnose, без поправок."""
    return puppets.diagnose()


def prepare(issues):
    """Готовить нечего: лечение каждого папета независимо, отказ одного
    не останавливает остальных."""
    return None, []


def treat(issue):
    """Каждый диагноз -- своим лечением puppets.treat."""
    return puppets.treat(issue)
