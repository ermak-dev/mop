"""The node itself: a body is the node, shared with every neighbour

Узел: тело папета — сам узел, общий со всеми соседями.

Сегодняшняя архитектура целиком, записанная как частный случай драйвера.
Изоляции между папетами одного узла нет никакой: клон в `~/puppets/<имя>`,
tmux-сокет в `/tmp/tmux-<uid>`, файл сессии в `~/.claude/sessions`,
транскрипты в `~/.claude/projects`. Ровно поэтому создание тела пустое, а
префикс команды — тоже пустой: исполнять «внутри тела» здесь значит исполнять
на узле.

Держать этот драйвер отдельным файлом, а не веткой `if driver == "host"`, —
единственный способ проверить шов до того, как в инвентаре появится
гипервизор: поведение обязано не измениться, и сравнивать надо с ним самим.
"""
import os

from .. import config
from . import sh, valid_name

HOME = config.get("MOP_HOME")
PREFIX = "pu-"
# Каталог сокетов tmux-серверов. У каждого папета свой сервер (-L <имя>),
# поэтому имя сокета и есть имя папета — отсюда и ростер без Nomad.
TMUX_DIR = os.environ.get("TMUX_TMPDIR") or f"/tmp/tmux-{os.getuid()}"

# session.py исполняется ВНУТРИ тела, а тело здесь — узел, поэтому путь
# берётся от самого пакета: он и есть тот, что приехал на узел rsync'ом.
SESSION_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "session.py")


def clone_dir(name):
    return f"{HOME}/puppets/{name}"


def target_dir(name):
    return f"{HOME}/.cache/target-{name}"


# ─── жизненный цикл тела ─────────────────────────────────────────────────
async def ensure(name, params=None):
    """Тело уже есть: это сам узел. Создавать нечего, и это не заглушка —
    это и есть ответ драйвера host на вопрос «в чём живёт папет»."""
    if not valid_name(name):
        return {"error": f"name {name!r} doesn't look like {PREFIX}<project>-<n>"}
    return {"name": name, "body": None, "created": False, "address": None}


async def destroy(name):
    """Снести тело. Узел снести нельзя — сносится всё, что папет в нём нажил.

    Клон не переклонируется — дорого и незачем: reset откатывает
    отслеживаемое, `clean -xdff` выметает и untracked, и игнорируемое
    (внутриклоновые кэши, node_modules), но `-e` защищает подсеянное врапером.
    Список исключений — из НАСТРОЙКИ MOP_PUPPET_SEED, той же, которой врапер
    сеет: два списка расползлись бы ровно к «посеяли одно, снесли другое».

    target-каталог — чисто производные данные, он уходит целиком, и с ним
    почти весь объём."""
    if not valid_name(name):
        return {"error": f"name {name!r} doesn't look like {PREFIX}<project>-<n>"}
    d = clone_dir(name)
    excl = " ".join(f"-e '{p}'" for p in
                    (s.strip() for s in config.get("MOP_PUPPET_SEED").split(",")) if p)
    out, code = await sh(f"git -C {d} reset --hard HEAD && "
                         f"git -C {d} clean -xdff {excl}")
    if code not in (0, None):
        return {"error": f"git in {d}: {out.strip() or f'exit {code}'}"}
    target = target_dir(name)
    # Долго: сотни тысяч inode. Таймаут шире офисного — обычный убил бы rm на
    # полпути и оставил каталог наполовину снесённым.
    _, code = await sh(f"rm -rf {target}", timeout=600)
    if code not in (0, None):
        return {"error": f"rm {target}: exit {code}"}
    return {"reset": True, "target": target}


async def bodies():
    """Папета, у которых на этом узле есть тело, — по сокетам tmux-серверов.

    Ростер без Nomad. У драйвера host «тело есть» означает ровно «сервер tmux
    поднят»: отдельного объекта, который можно было бы перечислить, здесь нет.
    """
    try:
        return sorted(n for n in os.listdir(TMUX_DIR) if n.startswith(PREFIX))
    except OSError:
        return []


async def capacity():
    """Память и место, которыми узел располагает под тела.

    У host хранилище тел — $HOME: и клоны, и target-каталоги лежат в нём. На
    этом числе стоит `mop gc`, поэтому отказ — это error, а не ноль: ноль
    означает измеренное «пусто»."""
    out, code = await sh(f"df -BG --output=avail,size {HOME}")
    if code not in (0, None) or not out.strip():
        return {"error": f"df did not answer: {out.strip() or f'exit {code}'}"}
    try:
        avail, size = out.splitlines()[1].split()
        disk = {"path": HOME, "free_gb": int(avail.rstrip("G")),
                "total_gb": int(size.rstrip("G"))}
    except (IndexError, ValueError):
        return {"error": f"df answered with garbage: {out.strip()!r}"}
    return disk


# ─── доступ в тело ───────────────────────────────────────────────────────
def argv(name):
    """Префикс команды. Пустой: исполнять в теле = исполнять на узле."""
    return []


def run_argv(name):
    """Чем узел запускает в теле врапер. Тоже пустой: врапер и так здесь."""
    return []


def repair_argv(name):
    """Аварийный путь. У host он совпадает с основным — второй дороги к узлу,
    на котором уже сидит агент, не бывает."""
    return []


def attach_argv(name):
    """Чем человек входит в сессию папета — изнутри узла.

    `mop attach` доводит человека до узла своим ssh и дальше исполняет это."""
    return ["tmux", "-L", name, "attach", "-t", name]


async def push(name, path, data):
    """Положить файл в тело: 600, атомарно.

    У host это обычная запись на узле. Белый список путей проверяет ЗВАВШИЙ:
    драйвер — транспорт, а не право."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                  "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception as e:
        return {"error": f"{path}: {e}"}
    return {"written": path}


def projects_dir(name):
    """Где лежат транскрипты папета — по ним считается расход токенов."""
    return f"{HOME}/.claude/projects"
