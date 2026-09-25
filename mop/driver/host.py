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
import json
import os
import shlex
import socket
from urllib.parse import urlparse

from ..common import busnames, config
from . import (HOME, PREFIX, bad_name, clone_dir, sh, target_dir, valid_name,
               why, write_private)

# Тело и узел — одна машина: исполнять «в теле» здесь значит исполнять на узле.
IS_CONTAINER = False
# Каталог сокетов tmux-серверов. У каждого папета свой сервер (-L <имя>),
# поэтому имя сокета и есть имя папета — отсюда и ростер без Nomad.
TMUX_DIR = os.environ.get("TMUX_TMPDIR") or f"/tmp/tmux-{os.getuid()}"

# session.py исполняется внутри тела, а тело здесь — узел, поэтому путь
# берётся от самого пакета: он и есть тот, что приехал на узел rsync'ом.
# Откуда узел знает адрес сервера: url его кредов шины (#200). MOP_SERVER_LAN
# в node.env не едет, а вторая его копия в NODE_SCOPED разъехалась бы с той,
# что плейбук уже рендерит в этот файл.
NODE_FILE = os.path.expanduser(busnames.NODE_FILE)

SESSION_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "session.py")


# ─── жизненный цикл тела ─────────────────────────────────────────────────
async def ensure(name, params=None):
    """Тело уже есть: это сам узел. Создавать нечего, и это не заглушка —
    это и есть ответ драйвера host на вопрос «в чём живёт папет»."""
    if not valid_name(name):
        return {"error": bad_name(name)}
    return {"name": name, "body": None, "created": False, "address": None}


async def destroy(name, branch=None):
    """Снести тело. Узел снести нельзя — сносится всё, что папет в нём нажил.

    Клон не переклонируется — дорого и незачем: reset откатывает
    отслеживаемое, `clean -xdff` выметает и untracked, и игнорируемое
    (внутриклоновые кэши, node_modules), но `-e` защищает подсеянное врапером.
    Список исключений — из настройки MOP_PUPPET_SEED, той же, которой врапер
    сеет: два списка расползлись бы ровно к «посеяли одно, снесли другое».

    target-каталог — чисто производные данные, он уходит целиком, и с ним
    почти весь объём."""
    if not valid_name(name):
        return {"error": bad_name(name)}
    d = clone_dir(name)
    excl = " ".join(f"-e '{p}'" for p in
                    (s.strip() for s in config.get("MOP_PUPPET_SEED").split(",")) if p)
    out, code = await sh(f"git -C {d} reset --hard HEAD && "
                         f"git -C {d} clean -xdff {excl}")
    # Таймаут -- отказ, а не успех (#171): здесь это недоделанный reset.
    if code != 0:
        return {"error": f"git in {d}: {why(out, code)}"}
    # Ветка мастера (#257): чистая рабочая копия начинает с неё. На origin
    # есть -- ровно её вершина (-B после fetch), нет -- завести локально от
    # HEAD, мастер создаст её первым landing. Клон не переклонируется, и
    # без этого шага он оставался бы на том, что застал.
    if branch:
        b = shlex.quote(branch)
        out, code = await sh(f"(git -C {d} fetch -q origin {b} && "
                             f"git -C {d} checkout -q -B {b} origin/{b}) || "
                             f"git -C {d} checkout -q -B {b}", timeout=120)
        if code != 0:
            return {"error": f"git checkout {branch} in {d}: {why(out, code, 120)}"}
    target = target_dir(name)
    # Долго: сотни тысяч inode. Таймаут шире офисного — обычный убил бы rm на
    # полпути и оставил каталог наполовину снесённым.
    _, code = await sh(f"rm -rf {target}", timeout=600)
    if code != 0:
        return {"error": f"rm {target}: {why('', code, 600)}"}
    return {"reset": True, "target": target}


def _server_alive(path):
    """Отвечает ли сервер tmux на этом сокете.

    Проверка соединением, а не запуском `tmux has-session`: перечисление тел
    зовут в горячих местах (ростер узла, раздача логина), и процесс на каждый
    сокет там лишний. Мёртвый сокет отказывает сразу, живой принимает
    соединение и тут же его закрывает — серверу это ничего не стоит."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        return s.connect_ex(path) == 0
    except OSError:
        return False
    finally:
        s.close()


async def bodies():
    """Папета, у которых на этом узле есть тело, — по сокетам tmux-серверов.

    Ростер без Nomad. У драйвера host «тело есть» означает ровно «сервер tmux
    поднят»: отдельного объекта, который можно было бы перечислить, здесь нет.

    Сокет переживает свой сервер, и одного его наличия мало. Пока здесь
    стоял голый listdir, узел объявлял телами файлы, оставшиеся от папетов
    месячной давности: на управляющей машине их набралось четырнадцать — от
    29 августа до 20 сентября, ни одного живого сервера, ни одного клона в
    ~/puppets. Уборка (`mop sweep`) честно прочитала их как сирот и
    попыталась снести несуществующие рабочие копии. Сами файлы безобидны:
    замерено, что tmux поднимает сервер на мёртвом сокете и переиспользует
    его, — поэтому их не убирают, их просто не считают телами."""
    try:
        names = [n for n in os.listdir(TMUX_DIR) if n.startswith(PREFIX)]
    except OSError:
        return []
    return sorted(n for n in names if _server_alive(os.path.join(TMUX_DIR, n)))


async def capacity():
    """Память и место, которыми узел располагает под тела.

    У host хранилище тел — $HOME: и клоны, и target-каталоги лежат в нём. На
    этом числе стоит `mop gc`, поэтому отказ — это error, а не ноль: ноль
    означает измеренное «пусто»."""
    out, code = await sh(f"df -BG --output=avail,size {HOME}")
    if code not in (0, None) or not out.strip():
        return {"error": f"df did not answer: {why(out, code)}"}
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

    У host это обычная запись на узле. Белый список путей проверяет звавший:
    драйвер — транспорт, а не право."""
    try:
        write_private(path, data)
    except Exception as e:
        return {"error": f"{path}: {e}"}
    return {"written": path}


async def push_many(name, files):
    """Несколько файлов в тело: у host это записи на узле, по одной."""
    for path, data in files:
        r = await push(name, path, data)
        if r.get("error"):
            return r
    return {"written": [p for p, _ in files]}


def projects_dir(name):
    """Где лежат транскрипты папета — по ним считается расход токенов."""
    return f"{HOME}/.claude/projects"


async def admit(name, let_in):
    """Серверу в узел дорога есть всегда: его ключ кладёт в authorized_keys
    пользователя пула `mop deploy` (роль bus). Открывать и закрывать нечего."""
    return {}


def _server():
    """Адрес сервера — хост из url кредов шины узла, либо RuntimeError.

    Не config.get("MOP_SERVER_LAN"): на узле этой настройки нет, и пустой
    адрес маршрутизировался к localhost — сервер получал 127.0.0.1 и ходил
    ssh'ем сам в себя (#200). В url плейбук кладёт LAN-адрес сервера
    (deploy/setup.yml, nats_lan) — тот, которым агент уже ходит на шину."""
    try:
        with open(NODE_FILE) as f:
            url = json.load(f).get("url") or ""
    except (OSError, ValueError, AttributeError) as e:
        raise RuntimeError(f"no server address: {NODE_FILE} is unreadable "
                           f"({e}) — run mop deploy")
    try:
        server = urlparse(url).hostname
    except ValueError:
        server = None
    if not server:
        raise RuntimeError(f"no server address: {NODE_FILE} has no host in its "
                           f"url {url!r} — run mop deploy")
    return server


def _ssh_port():
    """Порт sshd узла из MOP_SSH_PORT (node.env, #201), либо RuntimeError."""
    raw = config.get("MOP_SSH_PORT").strip()
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        raise RuntimeError(f"MOP_SSH_PORT={raw!r} in node.env is not a port — "
                           f"fix ansible_port in the inventory and run mop deploy")
    return int(raw)


def address(name):
    """Адрес тела со стороны сервера — это адрес узла, тот, с которого узел
    сам ходит на сервер. Без сети: соединение UDP ничего не шлёт, только
    выбирает маршрут.

    sshd узла не на 22 (#201) — адрес `адрес:порт`: сервер ходит на тело
    сам, и порт иначе знать неоткуда. На 22 — голый адрес, как было."""
    port = _ssh_port()
    server = _server()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((server, 1))
        here = s.getsockname()[0]
    except (socket.gaierror, OSError) as e:
        raise RuntimeError(f"no route to the server {server} (from {NODE_FILE}): {e}")
    finally:
        s.close()
    return here if port == 22 else f"{here}:{port}"


async def templates():
    """Сборочных тел у host не бывает: образ собирать не во что."""
    return []
