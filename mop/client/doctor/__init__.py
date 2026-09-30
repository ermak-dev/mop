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


# Что --fix --safe лечит (#359): расписанию -- только то, что не рвёт работу.
# login+nudge -- аренда из реестра и побудка, разговор переживает (#357);
# sweep -- у pu-sweep свои предохранители против сноса работы (#358).
# Рестарт, alloc stop, /model и перерегистрация сбрасывают сессию или
# решают за оператора -- их расписанный прогон только называет.
SAFE = ("login+nudge", "sweep")


def treats(issue, safe):
    """Лечит ли --fix (с --safe или без) эту проблему. Чистая функция."""
    return bool(issue["action"]) and (not safe or issue["action"] in SAFE)


def select(names, argv):
    """argv `mop doctor` -> ([группы], fix, safe). Чистая функция. Без группы
    -- все; одна названная -- она; иное -- ValueError с перечнем групп.
    --safe сужает лечение и без --fix смысла не имеет -- отказ."""
    fix, safe = "--fix" in argv, "--safe" in argv
    if safe and not fix:
        raise ValueError("--safe narrows --fix: mop doctor [group] --fix --safe")
    rest = [a for a in argv if a not in ("--fix", "--safe")]
    if not rest:
        return list(names), fix, safe
    if len(rest) == 1 and rest[0] in names:
        return rest, fix, safe
    raise ValueError(f"mop doctor [group] [--fix [--safe]]; the groups: {', '.join(names)}")
