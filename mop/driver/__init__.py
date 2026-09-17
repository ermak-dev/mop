"""Реестр драйверов узла: один файл в пакете — один драйвер.

Nomad решает, ГДЕ стоит папет, шина — КАК с ним говорить. Драйвер отвечает на
третий вопрос: В ЧЁМ он живёт. У драйвера `host` тело — сам узел, и вся
сегодняшняя архитектура становится его частным случаем, а не веткой, которую
надо поддерживать отдельно. План целиком — docs/DRIVER.md.

ДРАЙВЕР ВЫБИРАЕТ УЗЕЛ, А НЕ ПАПЕТ, и довод не вкусовой: Nomad выбирает узел
УЖЕ ПОСЛЕ регистрации спеки, а врапер лежит в спеке. Мастер в момент сборки
спеки не знает, на какой машине окажется папет, и заложить туда `pct exec` не
может. Источник правды — группа в инвентаре; `mop deploy` кладёт значение и в
`Environment=` юнита агента, и в `meta` клиента Nomad из одной переменной.
Агент драйвер ИЗ ЗАПРОСА не берёт: иначе мастер шарда A прислал бы своё
значение и заставил агента исполнить команду не там.

ТОЛЬКО STDLIB И `config`. Этот пакет живёт на узле рядом с агентом, а агенту
Nomad не нужен вовсе — в этом половина смысла переезда на шину. Импорт
`python-nomad` отсюда потянул бы его на каждый узел.

Контракт (docs/DRIVER.md), две половины в одном файле — узел и его тела:

    ensure(name, params)   поднять тело: клон шаблона, лимиты, адрес, старт
    destroy(name)          снести тело
    bodies()               что есть на этом узле — ростер без Nomad
    capacity()             память и место хранилища тел
    argv(name)             префикс команды: [] у host, ssh у контейнера
    run_argv(name)         чем узел запускает врапер в теле: соединение живёт
                           столько же, сколько папет
    push(name, path, data) положить файл внутрь (mop login)
    projects_dir(name)     где транскрипты — mop stat, usage
    attach_argv(name)      чем входит человек
    repair_argv(name)      аварийный путь, когда основной молчит
    SESSION_PY             путь к session.py ВНУТРИ тела

Разводить узел и тела по двум реестрам значит получить решётку «узел × тело»
и два места, отвечающих на один вопрос.
"""
import asyncio
import importlib
import os
import re
from pathlib import Path

# Глаголы контракта. Список закрыт и проверяется громко при загрузке: агент
# зовёт их из петли, и отсутствующий argv прочитается там как «узел молчит».
VERBS = ("ensure", "destroy", "bodies", "capacity", "argv", "run_argv", "push",
         "projects_dir", "attach_argv", "repair_argv")

DEFAULT = "host"

# Где узел хранит имя своего драйвера. ФАЙЛ, а не переменная окружения, и это
# не мелочь: драйвер спрашивают ДВОЕ — агент (юнит systemd) и внешний врапер
# (процесс задачи Nomad), — и окружение у них разное. Пока значение жило
# строкой Environment= в юните, врапер его не видел вовсе и молча поднимал
# папета драйвером host: на гипервизоре это значит «прямо на гипервизоре».
#
# Из ЗАПРОСА драйвер не берётся никогда — иначе мастер шарда A прислал бы своё
# значение и заставил узел исполнить команду не там. Кладёт файл `mop deploy`
# из той же переменной инвентаря, что и в meta клиента Nomad.
NODE_FILE = os.path.expanduser("~/.config/mop/driver")

# Имя папета склеивается в шелл — и у host, и у драйвера контейнеров, — а
# приезжает оно с шины. Проверка поэтому ОДНА, здесь: два списка допустимого
# разъехались бы молча, и разошлись бы они как раз на той стороне, где команда
# идёт внутрь чужой машины.
#
# Форма та же, что строит puppets.next_name: pu-<проект>-<n>.
_NAME = re.compile(r"^pu-[A-Za-z0-9][A-Za-z0-9._-]*-\d+$")

_CACHE = None


def valid_name(name):
    """Похоже ли это на имя папета. Всё, что не похоже, в шелл не попадает."""
    return bool(name) and bool(_NAME.match(name))


def contract(name, mod):
    """Модуль-плагин -> {verbs, session_py, doc}; RuntimeError при нарушении.

    Отдельная от загрузки функция, потому что проверяема без пула
    (tests/driver.py): ошибка контракта обязана находиться до живых папетов."""
    where = f"mop/driver/{name}.py"
    for verb in VERBS:
        fn = getattr(mod, verb, None)
        if not callable(fn):
            raise RuntimeError(f"{where}: no {verb}() — the contract is "
                               f"{', '.join(VERBS)} (docs/DRIVER.md)")
    session_py = getattr(mod, "SESSION_PY", None)
    # Абсолютный, потому что исполняется ВНУТРИ тела и из чужого каталога:
    # относительный там молча соберётся в `python3 session.py`, которого нет,
    # и живая сессия прочитается как мёртвая.
    if not isinstance(session_py, str) or not os.path.isabs(session_py):
        raise RuntimeError(f"{where}: SESSION_PY — absolute path to session.py "
                           f"INSIDE the body")
    doc = (mod.__doc__ or "").strip().splitlines()
    return {"verbs": {v: getattr(mod, v) for v in VERBS},
            "session_py": session_py,
            "doc": doc[0].strip() if doc else ""}


def drivers():
    """Весь реестр: {имя драйвера: контракт}. Имя файла = имя драйвера."""
    global _CACHE
    if _CACHE is None:
        _CACHE = {}
        for path in sorted(Path(__file__).parent.glob("*.py")):
            if path.stem.startswith("_"):
                continue
            mod = importlib.import_module(f".{path.stem}", __package__)
            _CACHE[path.stem] = contract(path.stem, mod)
    return _CACHE


def get(name):
    """Контракт драйвера по имени либо None."""
    return drivers().get(name)


def require(name):
    """Контракт по имени; громкий отказ с перечнем, если такого нет."""
    d = get(name)
    if d is None:
        raise RuntimeError(f"no node driver {name or '(empty)'}; available: "
                           f"{', '.join(drivers())} (docs/DRIVER.md)")
    return d


def module(name):
    """Сам модуль драйвера — после того как реестр проверил его контракт."""
    require(name)
    return importlib.import_module(f".{name}", __package__)


def current_name():
    """Драйвер ЭТОГО узла: окружение > файл узла > host.

    Окружение впереди ради разового прогона руками («а как этот узел выглядит
    драйвером pve»), файл — источник правды.

    Дефолт не косметика: узел, который про драйверы ничего не знает, обязан
    вести себя ровно как раньше — иначе раскатка шва стала бы раскаткой
    поведения."""
    env = os.environ.get("MOP_DRIVER")
    if env:
        return env
    try:
        with open(NODE_FILE) as f:
            return f.read().strip() or DEFAULT
    except OSError:
        return DEFAULT


def current():
    """Модуль драйвера этого узла."""
    return module(current_name())


# ─── исполнение ──────────────────────────────────────────────────────────
async def sh(script, timeout=20, prefix=()):
    """Шелл: на самом узле (пустой префикс) либо ВНУТРИ тела. -> (вывод, код).

    Скрипт всегда СТРОКА, а не список: префикс тела — это ssh, а ssh склеивает
    свои аргументы пробелом и отдаёт удалённому шеллу одной строкой. Список
    там молча пересобрался бы не в ту команду, поэтому форма одна и на узле, и
    в теле — строка, а кавычки расставляет вызывающий.

    Возврат (вывод, None) на таймауте отличается от (вывод, код): «не успел» и
    «ответил ненулевым» ведут в разные стороны, и молчащее тело нельзя
    прочитать как отказ команды."""
    if prefix:
        proc = await asyncio.create_subprocess_exec(
            *prefix, script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    else:
        proc = await asyncio.create_subprocess_shell(
            script, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return "", None
    return out.decode(errors="replace"), proc.returncode
