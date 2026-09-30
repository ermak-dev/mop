"""Группы проверок `mop doctor` (#356): один файл -- одна группа.

Устроено как драйверы узла (mop/driver/) и профили LLM (mop/common/llm/):
обнаружение глобом каталога (plugins.discover), таблицы регистрации нет,
контракт проверяется громко при загрузке, имя файла -- имя группы и слово
подкоманды: `mop doctor puppets`. Эпик #355 добавляет группы файлами, не
ветками в командлете.

Контракт группы -- модуль с докстрингом (первая строка -- строка группы в
отказе usage) и тремя функциями, все возвращают данные и молчат:
  diagnose()        -> [{name, alloc, diagnosis, action}]: action None --
                       лечения нет, решает оператор; проблема узла, а не
                       папета, несёт node вместо alloc (#358) -- колонку
                       «где» даёт where()
  prepare(issues)   -> (отказ | None, [строки]): что сделать до лечения
                       (раздать креды); отказ останавливает --fix целиком
  treat(issue)      -> что вышло, строкой

Потребитель (mop/cli/pool/doctor.py) зовёт только CONSUMED и по имени
группы не ветвится.
"""
import importlib

from mop.common import plugins

# Всё, что потребитель берёт у модуля группы. tests/doctor.py выводит этот
# список из кода потребителя и сверяет в обе стороны.
CONSUMED = ("diagnose", "prepare", "treat")

_CACHE = None


def contract(name, mod):
    """Модуль группы -> {doc}; RuntimeError при нарушении. Отдельно от
    загрузки: проверяема без пула (tests/doctor.py)."""
    where = f"mop/client/doctor/{name}.py"
    for verb in CONSUMED:
        if not callable(getattr(mod, verb, None)):
            raise RuntimeError(f"{where}: no {verb}() — the contract is "
                               f"{', '.join(CONSUMED)}")
    doc = (mod.__doc__ or "").strip().splitlines()
    if not doc:
        raise RuntimeError(f"{where}: no docstring — its first line names the group")
    return {"doc": doc[0].strip()}


def groups():
    """Весь реестр: {имя группы: контракт}, по алфавиту."""
    global _CACHE
    if _CACHE is None:
        _CACHE = plugins.discover(__file__, __package__, contract)
    return _CACHE


def module(name):
    """Модуль группы -- после того как реестр проверил его контракт."""
    if name not in groups():
        raise LookupError(f"no doctor group {name}")
    return importlib.import_module(f".{name}", __package__)


def where(issue):
    """Колонка «где» строки doctor: узел аллокации у проблемы папета, node --
    у проблемы самого узла (#358), иначе «-». Чистая функция."""
    if issue.get("alloc"):
        return issue["alloc"]["NodeName"]
    return issue.get("node") or "-"


def select(names, argv):
    """argv `mop doctor` -> ([группы], fix). Чистая функция. Без группы --
    все; одна названная -- она; иное -- ValueError с перечнем групп."""
    fix = "--fix" in argv
    rest = [a for a in argv if a != "--fix"]
    if not rest:
        return list(names), fix
    if len(rest) == 1 and rest[0] in names:
        return rest, fix
    raise ValueError(f"mop doctor [group] [--fix]; the groups: {', '.join(names)}")
