"""Канал сообщений: кому и как доставить, и что вышло.

Три пути, и граница между ними -- адрес (route):
  папет пула    `pu-<проект>-<n>`: через агента его узла, сокет host-local;
  сессия рядом  имя или pid сессии claude этого хоста: прямо в её сокет;
  мастер        всё остальное: его инбокс на шине `<логин>.<хост>-<pid>` (#213).

Жил во фронтенде MCP, а потолок ожидания и тексты отказа -- ещё и копией в
`mop send` (#148). Здесь вердикт доставки -- данные; фронтенды его только
рисуют. Протокол сокета -- mop/session.py (CHANNEL.md), субъекты -- BUS.md.
"""
import os

from mop.common import bus, puppets
from mop import session

# Дольше десяти минут ждать простоя незачем: запрос держит соединение, а
# уведомление о простое (notify_when_idle) не держит ничего.
MAX_WAIT = 600


def clamp_wait(seconds):
    """Сколько ждать простоя: 0..MAX_WAIT."""
    return min(max(seconds or 0, 0), MAX_WAIT)


# ─── адрес ───────────────────────────────────────────────────────────────
def is_puppet(to):
    """Папет узнаётся префиксом джоба -- это единственное имя, которое
    строит сам пул."""
    return to.startswith(puppets.JOB_PREFIX)


def local_session(target):
    """Сессия на этой машине или None, если её здесь нет.

    `Ambiguous` не глушим и наверх пускаем: «здесь такой нет» разрешает искать
    адресата дальше, на шине, а «их тут несколько» обязано остановить —
    молча взять первую значит однажды написать не тому."""
    try:
        return session.find(target)
    except session.Ambiguous:
        raise
    except LookupError:
        return None


def route(to):
    """Адрес -> ("puppet", имя) | ("local", файл сессии) | ("master", инбокс).

    Порядок не произвольный. Дальше папета пробуем свой хост: сессия рядом
    дешевле и не занимает шину. Всё остальное — мастер, потому что больше
    адресов не бывает, и «не нашёл здесь» обязано вести к нему, а не в
    LookupError: ровно этим отчёты папетов и терялись."""
    if is_puppet(to):
        return "puppet", to
    here = local_session(to)
    if here:
        return "local", here
    return "master", to


def puppet_node(name, master):
    """Узел папета — адрес на шине.

    У мастера узел берётся из ростера пула. На узле ростера нет, и мы
    спрашиваем сам пул: чей агент признаёт этот папет своим."""
    if master:
        return puppets.running_alloc(name)["NodeName"]
    for answer in bus.gather("local"):
        if name in (answer.get("puppets") or {}):
            return answer["node"]
    raise LookupError(f"{name}: no pool agent claims this puppet as its own")


# ─── своя сессия ─────────────────────────────────────────────────────────
def master_socket():
    """Инбокс сессии, которая нас запустила, — для асинхронных уведомлений.

    Основной путь — переменная окружения, которую claude кладёт потомкам.
    Запасной нужен, если окружение вычистили: идём вверх по цепочке
    родителей и ищем pid, у которого есть файл сессии."""
    sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    if sock:
        return sock
    by_pid = {str(d.get("pid")): d for d in session.sessions()}
    pid = os.getpid()
    for _ in range(24):
        try:
            with open(f"/proc/{pid}/status") as f:
                ppid = next(l.split()[1] for l in f if l.startswith("PPid:"))
        except (OSError, StopIteration):
            return None
        if ppid in by_pid:
            return by_pid[ppid]["messagingSocketPath"]
        pid = int(ppid)
        if pid <= 1:
            return None
    return None


def my_session():
    """Файл сессии, которая нас запустила: имя, каталог, pid."""
    sock = master_socket()
    if not sock:
        return None
    for d in session.sessions():
        if d.get("messagingSocketPath") == sock:
            return d
    return None


def my_name(master, master_id):
    """Как я представляюсь адресату — и каким именем он мне ответит.

    У мастера это его инбокс на шине (`master_id`): имя сессии claude адресом
    быть не может — оно выводится из каталога, не уникально и шине неизвестно.
    Оператор, спрашивавший «пингани их, пусть ответят», назвал папетам именно
    имя сессии, и ответить по нему было некуда.

    У папета это имя его джоба: оно же имя клона, им же папет адресуется в
    send. Берём из каталога сессии, а не из cwd процесса: сервер запускают
    откуда угодно, а сессия папета всегда стоит в своём клоне."""
    if master:
        return master_id
    cwd = (my_session() or {}).get("cwd") or os.getcwd()
    base = os.path.basename(os.path.realpath(cwd))
    return base if is_puppet(base) else master_id


# ─── доставка: вердикт, а не факт отправки ───────────────────────────────
# Вердикт -- dict: kind (puppet|local|master), to, и либо msg_id, либо error
# (отказ агента или мастера, в том числе владельца задания, #161), либо dead
# (инбокс сессии не слушает). Плюс owner_note, idle, wait, notify, pid.
# Сбой шины -- bus.BusError, как везде в библиотеке: `mop send` отдаёт его
# диспетчеру, MCP -- в вердикт через undelivered.


def send_to_puppet(node, name, message, priority="next", wait=0, notify=None,
                   **fields):
    """Доставка через агента узла. -> вердикт | bus.BusError.

    Ожидание простоя целиком на узле: подписку держит агент рядом с сокетом
    и, дождавшись, публикует в инбокс мастера. notify=None -- поле в запрос
    не идёт вовсе; с ожиданием подписка не нужна: ответ и так дождётся.
    Остальные поля (owner, force, from_name, reply_to) едут агенту как есть."""
    wait = clamp_wait(wait)
    extra = {} if notify is None else {"notify": bool(notify and not wait)}
    r = bus.request(node, "send", name=name, message=message, priority=priority,
                    wait=wait, **extra, **fields, timeout=wait + bus.TIMEOUT)
    v = {"kind": "puppet", "to": name, "wait": wait, "notify": extra.get("notify", False)}
    if "error" in r:
        return dict(v, error=r["error"])
    return dict(v, msg_id=r.get("msg_id"), owner_note=r.get("owner_note"),
                idle=r.get("idle"))


def send_local(sess, message, priority="next", wait=0, **fields):
    """Доставка в сессию этого хоста, прямо в её сокет. -> вердикт."""
    v = {"kind": "local", "to": sess.get("name"), "pid": sess.get("pid"),
         "wait": clamp_wait(wait)}
    sock = sess["messagingSocketPath"]
    if not session.socket_alive(sock):
        return dict(v, dead=True)
    r = session.send(sock, message, priority=priority, **fields, wait_idle=v["wait"])
    return dict(v, msg_id=r["msg_id"], idle=r["idle"])


def send_to_master(name, message, priority, from_name):
    """Обратный канал: папет -> мастер, в его инбокс на шине. -> вердикт |
    bus.BusError.

    Адрес мастера папет не придумывает: он приезжает в конверте каждого
    сообщения (from-name) и лежит в ростере. Ответа ждём — вердикт доставки
    здесь важнее, чем где-либо ещё: отчёт папета и есть главный сигнал петли,
    и «отправлено» вместо «доставлено» означало бы ровно то молчание, из-за
    которого мастер идёт читать чужой экран глазами."""
    # from — поле, а не имя параметра: в питоне это ключевое слово.
    r = bus.ask(name, "message", text=message, priority=priority, **{"from": from_name})
    v = {"kind": "master", "to": name}
    if "error" in r:
        return dict(v, error=r["error"])
    return dict(v, msg_id=r.get("msg_id"))


def undelivered(kind, to, error):
    """Вердикт из сбоя шины: для отправителя это тот же отказ."""
    return {"kind": kind, "to": to, "error": str(error)}


# ─── вердикт -> текст ────────────────────────────────────────────────────
def failure(v):
    """Строка отказа, либо None, если доставлено."""
    if v.get("dead"):
        return f"{v['to']}: inbox not listening — session is dead"
    if v.get("error") is not None:
        return f"{v['to']}: NOT DELIVERED — {v['error']}"
    return None


def text(v):
    """Вердикт одной строкой -- так его читает модель."""
    why = failure(v)
    if why:
        return why
    out = f"{v['to']}: delivered (msg_id={v.get('msg_id')})"
    wait = v.get("wait")
    if v["kind"] == "puppet":
        if v.get("owner_note"):
            out += f"; {v['owner_note']}"
        if wait:
            out += f", idle: {v.get('idle') or 'did not wait it out in ' + str(wait) + 's'}"
        if v.get("notify"):
            out += "; will notify when it frees up"
    elif v["kind"] == "local" and wait:
        out += f", idle: {(v.get('idle') or {}).get('state') or 'did not wait it out'}"
    return out
