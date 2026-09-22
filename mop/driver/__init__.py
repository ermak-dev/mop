"""Реестр драйверов узла: один файл в пакете — один драйвер.

Nomad решает, где стоит папет, шина — как с ним говорить. Драйвер отвечает на
третий вопрос: в чём он живёт. У драйвера `host` тело — сам узел, и вся
сегодняшняя архитектура становится его частным случаем, а не веткой, которую
надо поддерживать отдельно. План целиком — docs/DRIVER.md.

Драйвер выбирает узел, а не папет, и довод не вкусовой: Nomad выбирает узел
уже после регистрации спеки, а врапер лежит в спеке. Мастер в момент сборки
спеки не знает, на какой машине окажется папет, и заложить туда `pct exec` не
может. Источник правды — группа в инвентаре; `mop deploy` кладёт значение и в
`Environment=` юнита агента, и в `meta` клиента Nomad из одной переменной.
Агент драйвер из запроса не берёт: иначе мастер шарда A прислал бы своё
значение и заставил агента исполнить команду не там.

Только STDLIB И `config`. Этот пакет живёт на узле рядом с агентом, а агенту
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
    SESSION_PY             путь к session.py внутри тела
    BODY_IS_NODE           тело и узел — одна машина (True у host)

Разводить узел и тела по двум реестрам значит получить решётку «узел × тело»
и два места, отвечающих на один вопрос.
"""
import asyncio
import importlib
import os
import re

from .. import config, plugins

# Глаголы контракта. Список закрыт и проверяется громко при загрузке: агент
# зовёт их из петли, и отсутствующий argv прочитается там как «узел молчит».
VERBS = ("ensure", "destroy", "bodies", "capacity", "argv", "run_argv", "push",
         "projects_dir", "attach_argv", "repair_argv")

DEFAULT = config.SETTINGS["MOP_DRIVER"]

# Соглашение об имени папета живёт здесь, потому что здесь его читают обе
# стороны. Форма pu-<проект>-<n> строится у мастера (puppets.next_name), а
# разбирается на узле: агентом, драйверами, внешним врапером. Узлу puppets не
# импортировать — он тянет python-nomad, — и пока общего stdlib-дома не было,
# каждый читатель держал свою копию префикса и своего rsplit (#47).
#
# Префикс не настраивается: на нём стоят глобы сторожа диска (включая
# переходные ~/wk/wk-*) и имена tmux-серверов. Сделать его переменной, пока
# сторож знает оба префикса буквально, — значит развести половины одного
# соглашения.
PREFIX = "pu-"

# Имя папета склеивается в шелл — и у host, и у драйвера контейнеров, — а
# приезжает оно с шины. Проверка поэтому одна, здесь: два списка допустимого
# разъехались бы молча, и разошлись бы они как раз на той стороне, где команда
# идёт внутрь чужой машины.
_NAME = re.compile(rf"^{PREFIX}[A-Za-z0-9][A-Za-z0-9._-]*-\d+$")

_CACHE = None


def valid_name(name):
    """Похоже ли это на имя папета. Всё, что не похоже, в шелл не попадает."""
    return bool(name) and bool(_NAME.match(name))


def bad_name(name):
    """Текст отказа по имени — один на все места, где имя проверяют."""
    return f"name {name!r} doesn't look like {PREFIX}<project>-<n>"


def shard_of_name(name):
    """Шард по имени папета: pu-<проект>-<n>. Откат для случая, когда клона
    ещё нет, — origin спросить не у кого, а имя уже есть. Без префикса —
    пусто, а не кусок чужой строки."""
    if not name.startswith(PREFIX):
        return ""
    return name[len(PREFIX):].rsplit("-", 1)[0]


# Где папет живёт в теле. Один путь и у мастера (mcp, delete называют его
# человеку), и у узла (агент меряет и пробует), и у врапера в спеке.
HOME = config.get("MOP_HOME")


def clone_dir(name):
    return f"{HOME}/puppets/{name}"


def target_dir(name):
    return f"{HOME}/.cache/target-{name}"


def why(out, code):
    """Причина отказа шелла одной строкой: вывод, а если он пуст — код."""
    return out.strip() or f"exit {code}"


def write_private(path, data):
    """Файл 600, атомарно: во временный рядом и rename. Так узел кладёт креды
    (агент, глагол write) и так драйвер host кладёт файл в своё тело — это
    одна и та же запись, и была скопирована дословно."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
              "wb") as f:
        f.write(data)
    os.replace(tmp, path)


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
    # Одно тело на узел или много — это разные вопросы к одному драйверу, и
    # спрашивать «пустой ли argv» вместо ответа значит выводить свойство из
    # побочного признака. Раздача файлов (`mop login`) на этом стоит: у host
    # запись в каждое тело была бы записью в тот же файл по разу на папета, а
    # отчёт обещал бы запись в тела, которых нет.
    if not isinstance(getattr(mod, "BODY_IS_NODE", None), bool):
        raise RuntimeError(f"{where}: BODY_IS_NODE — True when the body is the "
                           f"node itself, False when it is a thing of its own")
    session_py = getattr(mod, "SESSION_PY", None)
    # Абсолютный, потому что исполняется внутри тела и из чужого каталога:
    # относительный там молча соберётся в `python3 session.py`, которого нет,
    # и живая сессия прочитается как мёртвая.
    if not isinstance(session_py, str) or not os.path.isabs(session_py):
        raise RuntimeError(f"{where}: SESSION_PY — absolute path to session.py "
                           f"inside the body")
    doc = (mod.__doc__ or "").strip().splitlines()
    return {"verbs": {v: getattr(mod, v) for v in VERBS},
            "session_py": session_py,
            "body_is_node": mod.BODY_IS_NODE,
            "doc": doc[0].strip() if doc else ""}


def drivers():
    """Весь реестр: {имя драйвера: контракт}. Имя файла = имя драйвера."""
    global _CACHE
    if _CACHE is None:
        _CACHE = plugins.discover(__file__, __package__, contract)
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
    """Драйвер этого узла — обычная узловая настройка (config.NODE_SCOPED).

    Спрашивают её двое с разным окружением: агент из юнита systemd и внешний
    врапер из процесса задачи Nomad. Пока значение жило строкой Environment= в
    юните, врапер его не видел вовсе и молча поднимал папета драйвером host —
    на гипервизоре это значит «прямо на гипервизоре, мимо тела».

    Из запроса не берётся никогда: иначе мастер шарда A прислал бы своё
    значение и заставил узел исполнить команду не там."""
    return config.get("MOP_DRIVER") or DEFAULT


def current():
    """Модуль драйвера этого узла."""
    return module(current_name())


# ─── исполнение ──────────────────────────────────────────────────────────
async def sh(script, timeout=20, prefix=()):
    """Шелл: на самом узле (пустой префикс) либо внутри тела. -> (вывод, код).

    Скрипт всегда строка, а не список: префикс тела — это ssh, а ssh склеивает
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
