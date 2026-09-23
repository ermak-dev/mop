"""pool MCP server: the same pool as tools for claude

Запускается как `mop mcp`, транспорт stdio.

Транспорт stdio, запускается дочерним процессом сессии — мастера или папета.

Три профиля, и решают их креды на шине, а не флаги (is_master):

  * кред оператора + MOP_PROJECT -> мастер проекта: канал и управление, но
    только своим срезом пула (`mop master` ставит проект);
  * кред оператора без проекта -> оператор: то же самое, но по всему пулу;
  * кред `puppet-<проект>` -> узел: один канал, ростер -- опросом самой шины.

Управление -- сами командлеты (#160): командлет с объявлением `MCP = {...}`
становится инструментом, своей реализации операций здесь нет.

Так узел физически не может позвать то, чего ему не положено, вместо того
чтобы не звать по уговору.

Почему не встроенный SendMessage: он видит только этот хост плюс облачные и
bridge-сессии, а папета пула живут на других узлах. Здесь доставка идёт через
шину, и хост значения не имеет.

Канал двусторонний. У `send` три адреса, а не два: папет пула, сессия этого
хоста и мастер — по его инбоксу на шине. Третьего не было, и петля мастера
стояла на песке: папет отчитывался, `session.find` не находил мастера на своём
хосте, исключение приезжало модели строкой «Error executing tool send», а
мастер читал молчание и шёл смотреть чужой экран глазами.

Сокет сессии host-local, поэтому до него дотягивается агент, который на том же
узле и живёт (mop/agent.py). Протокол канала — CHANNEL.md, субъекты шины —
BUS.md, устройство сервера — MCP.md.
"""
import functools
import inspect
import os
import subprocess
import threading
from typing import Annotated, Optional


from mcp.server.mcpserver import MCPServer                     # noqa: E402
from mcp.types import ToolAnnotations                          # noqa: E402
from pydantic import Field                                     # noqa: E402

from mop import bus, cli, session, puppets                # noqa: E402
from mop.cli import lib                                   # noqa: E402
from mop.render import table                              # noqa: E402

app = MCPServer(
    "mop",
    instructions=(
        "Pool of claude puppets on top of Nomad.\n\n"
        "THIS IS THE ONLY CHANNEL TO THE POOL. To message a puppet or find out "
        "who's free, use `send` and `agents` from here. The built-in "
        "SendMessage/ListAgents won't do: they only see sessions on THIS SAME "
        "host, and puppets live on other nodes — on a neighboring node the "
        "built-in lookup will silently find no one. Delivery here goes through "
        "the pool's bus, and the host doesn't matter.\n\n"
        "`agents` — who's around and in what state, `send` — message a puppet, "
        "`tail` — what's on its screen. Always check `agents` before "
        "dispatching: \"free\" there means the clone has no unsaved work, not "
        "just that the session is silent.\n\n"
        "The other tools are the mop commands themselves, run from this "
        "session's working copy: their output is the answer, and a command "
        "that succeeds quietly answers `done` — the result shows in `agents`."
    ),
)

def is_master():
    """Есть ли у нас право управлять пулом. Смотрим, ЧЬИ У НАС КРЕДЫ.

    Это факт, а не флаг: пользователь NATS и есть набор прав. `puppet-<проект>`
    в субъект сервиса кластера не пишет вовсе (nats-server.conf), `admin` и
    `master-<проект>` пишут — то есть вопрос «мастер ли мы» и вопрос «под кем мы
    на шине» это один вопрос.

    Раньше здесь лежала проверка «есть ли токен Nomad». Она отвечала верно
    ровно потому, что токен возили только мастерам; с уходом токена с машин
    операторов (#82) она отвечала бы «папет» всем сразу. Спрашивать саму шину
    тоже можно, но это секунды на старте каждого папета и отказ прав в логе
    на ровном месте — а ответ уже лежит в кредах, локально."""
    try:
        return not bus.config()["user"].startswith("puppet-")
    except Exception:
        return False


MASTER = is_master()
# Свой адрес для вестей от агентов. Уникален на процесс: два терминала,
# открытые в одном проекте, иначе разбирали бы вести друг друга.
MASTER_ID = f"{os.uname().nodename}-{os.getpid()}"
MY_INBOX = bus.inbox(MASTER_ID)


def loud(fn):
    """Причина отказа — в ответ, а не в исключение.

    Исключение из инструмента приезжает модели строкой «Error executing tool
    send» без причины, и это худший вид отказа: папет, у которого не было
    маршрута до мастера, увидел ровно её — три попытки, три разных адреса,
    один и тот же глухой текст и никакой возможности понять, что дело в
    адресе. Инструмент обязан называть себя и причину."""
    @functools.wraps(fn)
    def wrap(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            return f"{fn.__name__}: {e or type(e).__name__}"
    return wrap


def tool(**kw):
    """Инструмент сервера. Тело всегда под loud."""
    def deco(fn):
        return app.tool(**kw)(loud(fn))
    return deco


def master_tool(**kw):
    """Инструмент, который есть только у мастера. На узле функция остаётся
    обычной питоновской и просто не попадает в список инструментов."""
    def deco(fn):
        return tool(**kw)(fn) if MASTER else fn
    return deco


READ_ONLY = ToolAnnotations(readOnlyHint=True)
DESTRUCTIVE = ToolAnnotations(destructiveHint=True)
SLASH_ALLOWED = ("/model", "/clear", "/compact", "/rc", "/status", "Escape")
MAX_WAIT = 600
# Опрос мастеров короче обычного запроса: мастер отвечает из памяти, а ждёт
# его каждый вызов agents — gather не знает, сколько ответов ему ждать, и
# честно досиживает до таймаута.
MASTERS_WAIT = 2


# ─── адресация ───────────────────────────────────────────────────────────
def puppet_alloc(name):
    """Живая аллокация папета. Путь к сокету никогда не кэшируем: после
    рестарта папета меняется pid, а с ним и имя сокета."""
    alloc = bus.ask_cluster("alloc", name=name).get("alloc")
    if not alloc:
        raise LookupError(f"{name}: no allocation — puppet not placed")
    if alloc["ClientStatus"] != "running":
        raise LookupError(f"{name}: allocation {alloc['ClientStatus']}, not running")
    return alloc


def puppet_node(name):
    """Узел папета — адрес на шине.

    У мастера узел берётся из ростера пула. На узле ростера нет, и мы
    спрашиваем сам пул: чей агент признаёт этот папет своим."""
    if MASTER:
        return puppet_alloc(name)["NodeName"]
    for answer in bus.gather("local"):
        if name in (answer.get("puppets") or {}):
            return answer["node"]
    raise LookupError(f"{name}: no pool agent claims this puppet as its own")


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


def my_name():
    """Как я представляюсь адресату — и каким именем он мне ответит.

    У мастера это его инбокс на шине (`MASTER_ID`): имя сессии claude адресом
    быть не может — оно выводится из каталога, не уникально и шине неизвестно.
    Оператор, спрашивавший «пингани их, пусть ответят», назвал папетам именно
    имя сессии, и ответить по нему было некуда.

    У папета это имя его джоба: оно же имя клона, им же папет адресуется в
    send. Берём из каталога сессии, а не из cwd процесса: сервер запускают
    откуда угодно, а сессия папета всегда стоит в своём клоне."""
    if MASTER:
        return MASTER_ID
    cwd = (my_session() or {}).get("cwd") or os.getcwd()
    base = os.path.basename(os.path.realpath(cwd))
    return base if base.startswith(puppets.JOB_PREFIX) else MASTER_ID


def on_inbox(m):
    """Что приехало в инбокс мастера: вести агентов и отчёты папетов.

    Возвращённое становится ответом на запрос — так у папета появляется вердикт
    доставки вместо факта отправки. Агент вести публикует и ответа не ждёт:
    для него не меняется ничего.

    `who` — единственный глагол, который не кладут в сессию: им спрашивают
    общий инбокс проекта, «кто из мастеров жив»."""
    if m.get("verb") == "who":
        d = my_session() or {}
        return {"master": MASTER_ID, "project": bus.PROJECT,
                "session": d.get("name"), "cwd": d.get("cwd")}
    sock = master_socket()
    if not sock:
        return {"error": f"master {MASTER_ID} has no session — nowhere to deliver the note"}
    # priority по умолчанию later: так кладёт вести агент, и перебивать ход
    # мастера уведомлением о простое незачем. Папет со своим отчётом просит
    # громче — и имеет право, отчёта мастер ждёт.
    r = session.send(sock, m.get("text") or "",
                     priority=m.get("priority") or "later",
                     from_name=m.get("from") or m.get("node") or "mop")
    return {"msg_id": r["msg_id"]}


# ─── инструменты: канал ──────────────────────────────────────────────────
@tool(annotations=READ_ONLY, description=(
    "Who you can message: pool puppets on ALL nodes, masters of your project, "
    "and claude sessions on this machine. Fuller than the built-in "
    "ListAgents, which only sees this host. For a puppet it shows the true "
    "state (free/busy/HUNG/no model quota/unsaved work in the clone), "
    "LLM profile, and repository; for a master, the address to reply to."))
def agents(project: str = "") -> str:
    out = _roster(project) if MASTER else _roster_from_bus()
    out += _masters()

    here = [(d.get("name") or "-", str(d.get("pid")), d.get("status") or "-",
              d.get("cwd") or "-")
             for d in session.sessions()
             if session.socket_alive(d["messagingSocketPath"])]
    if here:
        out += ["", "sessions on this machine:",
                *table([("SESSION", "PID", "STATUS", "DIRECTORY"), *here])]
    return "\n".join(out)


def _roster(project):
    """Ростер мастера: Nomad знает про папета то, чего не знает узел, —
    состояние аллокации, профиль LLM, репозиторий."""
    rows = [("PUPPET", "NODE", "ALLOC", "STATE", "LLM", "REPOSITORY")]
    for r in puppets.puppet_rows():
        if project and project not in r["origin"]:
            continue
        rows.append((r["name"], r["node"], r["alloc_status"], r["state"],
                     r["llm"], r["origin"]))
    return ["pool puppets:", *table(rows)] if len(rows) > 1 else ["pool puppets: none"]


def _roster_from_bus():
    """Ростер узла: у него нет токена Nomad, поэтому пул опрашивается сам.

    Колонок меньше, и это честно: аллокацию и репозиторий узлу взять неоткуда.
    Для того, ради чего папет смотрит в agents — кому написать и кто свободен, —
    хватает имени, узла и состояния."""
    rows = [("PUPPET", "NODE", "STATE")]
    try:
        answers = bus.gather("local")
    except bus.BusError as e:
        return [f"bus unavailable: {e}"]
    for answer in sorted(answers, key=lambda a: a.get("node") or ""):
        for name, facts in sorted((answer.get("puppets") or {}).items()):
            rows.append((name, answer.get("node") or "?", puppets.puppet_state(facts)))
    return ["pool puppets:", *table(rows)] if len(rows) > 1 else ["pool puppets: none"]


def _masters():
    """Мастера проекта: кому отсюда можно ответить.

    Реестра нет намеренно — спрашиваем общий инбокс проекта, и живой мастер это
    тот, кто отозвался. Папету эта строка нужна как воздух: без неё адрес
    мастера негде взять, кроме конверта уже полученного письма.

    Мастеру она показывает соседа. Мастеров на проект можно держать сколько
    угодно, но раздавать задачи вдвоём, не зная друг о друге, нельзя: два пинга
    одному папету в один вечер как раз этим и кончились."""
    try:
        found = bus.gather("who", timeout=MASTERS_WAIT,
                           subj=bus.inbox(bus.ALL_MASTERS))
    except bus.BusError as e:
        return ["", f"masters of project {bus.PROJECT}: bus unavailable ({e})"]
    if not found:
        return ["", f"no masters of project {bus.PROJECT} on the bus"]
    rows = [("MASTER (address for send)", "SESSION", "DIRECTORY")]
    for d in sorted(found, key=lambda d: str(d.get("master"))):
        mine = " (this is me)" if d.get("master") == MASTER_ID else ""
        rows.append((str(d.get("master")), (d.get("session") or "-") + mine,
                     d.get("cwd") or "-"))
    return ["", "masters of project:", *table(rows)]


@tool(description=(
    "Send a message to a pool puppet, a project master, or a session on this "
    "machine. The ONLY working way to reach both a puppet and a master: the "
    "built-in SendMessage only reaches sessions on this same host and on a "
    "neighboring node will silently find no one. `to`: puppet name "
    "(pu-<project>-<n>), master address from agents or from the envelope of "
    "a received message (from-name), name/pid of a local session. Returns a "
    "delivery verdict, not the fact of sending. notify_when_idle=true does "
    "not block: when the recipient frees up, a notification arrives as a "
    "separate message."))
def send(to: str, message: str, priority: str = "next",
         notify_when_idle: bool = False, wait_seconds: int = 0) -> str:
    if priority not in session.PRIORITIES:
        return f"priority must be one of {', '.join(session.PRIORITIES)}"
    if not message.strip():
        return "empty message, nothing to send"

    # Три адреса, и порядок разбора не произвольный. Папет узнаётся префиксом
    # джоба — это единственное имя, которое строит сам пул. Дальше пробуем свой
    # хост: сессия рядом дешевле и не занимает шину. Всё остальное — мастер,
    # потому что больше адресов не бывает, и «не нашёл здесь» обязано вести к
    # нему, а не в LookupError: ровно этим отчёты папетов и терялись.
    if to.startswith(puppets.JOB_PREFIX):
        return _send_to_puppet(to, message, priority, notify_when_idle, wait_seconds)
    here = _local_session(to)
    if here:
        return _send_locally(here, message, priority, wait_seconds)
    return _send_to_master(to, message, priority)


def _local_session(target):
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


def _send_to_master(name, message, priority):
    """Обратный канал: папет -> мастер, в его инбокс на шине.

    Адрес мастера папет не придумывает: он приезжает в конверте каждого
    сообщения (from-name) и лежит в ростере. Ответа ждём — вердикт доставки
    здесь важнее, чем где-либо ещё: отчёт папета и есть главный сигнал петли,
    и «отправлено» вместо «доставлено» означало бы ровно то молчание, из-за
    которого мастер идёт читать чужой экран глазами."""
    try:
        # from — поле, а не имя параметра: в питоне это ключевое слово.
        r = bus.ask(name, "message", text=message, priority=priority,
                    **{"from": my_name()})
    except bus.BusError as e:
        return f"{name}: NOT DELIVERED — {e}"
    if "error" in r:
        return f"{name}: NOT DELIVERED — {r['error']}"
    return f"{name}: delivered (msg_id={r.get('msg_id')})"


def _send_to_puppet(name, message, priority, notify_when_idle, wait_seconds):
    """Доставка через агента узла.

    Ожидание простоя целиком уехало на узел: подписку держит агент рядом с
    сокетом и, дождавшись, публикует в инбокс мастера. Здесь больше нет
    фонового потока — он ждал внутри аллокации и умирал вместе с ней."""
    wait = min(max(wait_seconds, 0), MAX_WAIT)
    try:
        result = bus.request(puppet_node(name), "send", name=name, message=message,
                             priority=priority, wait=wait,
                             notify=bool(notify_when_idle and not wait),
                             # Кто пишет. Едет в конверт полем from-name, и
                             # подсказка канала называет его папету как адрес
                             # для ответа: без этого «ответь мне» указывает в
                             # никуда, а папет узнаёт об этом уже отказом.
                             from_name=my_name(),
                             reply_to=MY_INBOX, timeout=wait + bus.TIMEOUT)
    except bus.BusError as e:
        return f"{name}: NOT DELIVERED — {e}"
    if "error" in result:
        return f"{name}: NOT DELIVERED — {result['error']}"

    verdict = f"{name}: delivered (msg_id={result['msg_id']})"
    if wait:
        verdict += f", idle: {result.get('idle') or 'did not wait it out in ' + str(wait) + 's'}"
    if notify_when_idle and not wait:
        verdict += "; will notify when it frees up"
    return verdict


def _send_locally(sess, message, priority, wait_seconds):
    sock = sess["messagingSocketPath"]
    if not session.socket_alive(sock):
        return f"{sess.get('name')}: inbox not listening — session is dead"
    r = session.send(sock, message, priority=priority, from_name=my_name(),
                     wait_idle=min(max(wait_seconds, 0), MAX_WAIT))
    verdict = f"{sess.get('name')}: delivered (msg_id={r['msg_id']})"
    if wait_seconds:
        verdict += f", idle: {(r['idle'] or {}).get('state') or 'did not wait it out'}"
    return verdict


@tool(annotations=READ_ONLY, description=(
    "Tail of the puppet's tmux buffer — the only way to see what's not in "
    "the state: a held dialog for someone else's message, a login prompt, "
    "a quota complaint."))
def tail(name: str, lines: int = 40, grep: str = "") -> str:
    buf = puppets.pane_lines(puppet_node(name), name)
    if grep:
        buf = [l for l in buf if grep.lower() in l.lower()]
    return "\n".join(buf[-max(1, lines):]) or "(empty)"


# ─── инструменты: управление ─────────────────────────────────────────────
@master_tool(annotations=DESTRUCTIVE, description=(
    "Slash command in a puppet's TUI. Needed separately because slash "
    "commands don't pass through the channel: the message is queued with "
    "skipSlashCommands. Escape DISMISSES a stuck dialog without answering "
    "it: a stuck puppet is invisible to the roster, and messages pile up "
    f"unread in the queue. Allowed: {', '.join(SLASH_ALLOWED)}."))
def slash(name: str, command: str) -> str:
    if not command.split()[0:1] or command.split()[0] not in SLASH_ALLOWED:
        return f"only allowed: {', '.join(SLASH_ALLOWED)}"
    node = puppet_node(name)
    if command.startswith("/model "):
        puppets.switch_model(node, name, command.split(None, 1)[1])
        return f"{name}: {command}"
    return f"{name}: {command}\n{puppets.type_command(node, name, command)[-1500:]}"


# ─── инструменты: командлеты (#160) ──────────────────────────────────────
# Управление пулом -- не вторая реализация, а сами командлеты. Командлет,
# объявивший `MCP = {...}` (mop/cli/__init__.py), становится инструментом:
# описание -- его докстринг, вызов -- `mop <команда>` подпроцессом из
# каталога сессии мастера, ответ -- его вывод как есть. Своя копия add и
# build здесь уже разошлась однажды с командлетами (workspace, сборщик);
# копии нет -- расходиться нечему.
MOP = os.path.join(cli.BIN, "mop")
COMMAND_TIMEOUT = 600
TYPES = {"string": str, "integer": int, "boolean": bool}
HINTS = {"readonly": READ_ONLY, "destructive": DESTRUCTIVE}


def run_command(words, argv):
    """`mop <слова> <argv>` из каталога сессии -> ответ модели.

    Каталог -- сессии, а не процесса: сервер запускают откуда угодно, а
    origin и workspace командлет берёт из рабочей копии, в которой стоит
    мастер. Вывод отдаётся как есть, без разбора: он английский и читается
    моделью (CLAUDE.md), а цвета терминала снимаются."""
    cwd = (my_session() or {}).get("cwd") or os.getcwd()
    try:
        r = subprocess.run([MOP, *words, *argv], cwd=cwd, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"mop {' '.join(words)}: no answer in {COMMAND_TIMEOUT}s — still running"
    text = lib.plain(r.stdout + r.stderr).strip()
    if r.returncode:
        return f"mop {' '.join(words)}: FAILED (exit {r.returncode})\n{text}".rstrip()
    return text or "done"


def run_in_background(words, argv):
    """Долгая команда: ответ сразу, итог -- вестью в сессию, тем же путём,
    каким приезжают вести агентов (on_inbox)."""
    def go():
        text = f"mop {' '.join(words)}: {run_command(words, argv)}"
        sock = master_socket()
        if sock:
            try:
                session.send(sock, text, priority="later", from_name="mop")
            except Exception:
                pass
    threading.Thread(target=go, daemon=True, name=f"mop-{'-'.join(words)}").start()
    return (f"mop {' '.join(words)}: started; the outcome arrives in this "
            f"session as a separate message")


def command_tool(name, words, path, decl):
    """Инструмент из объявления командлета. Сигнатура собирается из args:
    по ней SDK строит схему, которую видит модель."""
    args = decl.get("args", [])

    def call(**values):
        argv = cli.tool_argv(args, values)
        run = run_in_background if decl.get("background") else run_command
        return run(words, argv)

    params = []
    for a in args:
        t = TYPES[a["type"]]
        default = inspect.Parameter.empty if a.get("required") else \
            False if t is bool else None
        hint = t if a.get("required") or t is bool else Optional[t]
        params.append(inspect.Parameter(
            a["name"], inspect.Parameter.KEYWORD_ONLY, default=default,
            annotation=Annotated[hint, Field(description=a.get("help", ""))]))
    call.__name__ = name
    call.__signature__ = inspect.Signature(params, return_annotation=str)
    if name in {t.name for t in app._tool_manager.list_tools()}:
        raise RuntimeError(f"tool {name} is defined twice: by hand and by {path}")
    app.add_tool(loud(call), name=name, description=cli.docstring(path),
                 annotations=HINTS.get(decl.get("annotations")))


def command_tools():
    """Все объявленные командлеты -- инструменты. Только у мастера: узлу
    управление не положено, и его креды в сервис кластера не пишут."""
    if not MASTER:
        return
    for name, words, path in cli.tool_commands(cli.scan(), cli.verbs()):
        decl = cli.declared(path)
        if decl is not None:
            command_tool(name, words, path, decl)


command_tools()


def watch_inbox():
    """Подписки мастера: свой инбокс и общий инбокс проекта.

    MCP умеет только «запрос-ответ» — вбросить что-то в ход модели сервер не
    может. Но он дочерний процесс сессии и знает её сокет, поэтому кладёт
    туда. Раньше на этом месте был поток, ждавший простоя внутри аллокации;
    теперь ждёт агент рядом с сокетом папета, а сюда приезжает готовая весть.
    Этим же субъектом папет отвечает мастеру — обратная сторона канала.

    Вторая подписка (`master.all.inbox`) — это весь механизм обнаружения:
    папет спрашивает её глаголом `who` и узнаёт, кому он может ответить.
    Реестра мастеров нет и не нужно: мастер живёт ровно столько, сколько живёт
    его сессия, а любой реестр пережил бы её и врал.

    Молчим, если сессии нет: мастер, которому некуда положить весть, не имеет
    права называться живым адресом — папет получит честное «нет на шине»
    вместо доставленного в никуда."""
    if not MASTER or not master_socket():
        return
    try:
        bus.subscribe(MY_INBOX, on_inbox)
        bus.subscribe(bus.inbox(bus.ALL_MASTERS), on_inbox)
    except bus.BusError:
        # Без шины сервер всё равно поднимется: доставка отобьётся понятной
        # ошибкой на первом же send, а молчащая подписка не соврёт успехом.
        pass


def main(argv=None):
    # attach остаётся вне MCP: живой терминал инструментом не отдать.
    if "--check" in (argv or []):
        try:
            where = bus.config()["url"]
        except bus.BusError as e:
            where = f"NONE ({e})"
        who = ("node" if not MASTER
               else "operator" if bus.PROJECT == bus.ADMIN
               else f"master of project {bus.PROJECT}")
        print(f"mop mcp: profile {who}, "
              f"{len(app._tool_manager.list_tools())} tools, bus {where}, "
              f"address {my_name()}, inbox {MY_INBOX if MASTER else '(not a master)'}, "
              f"session: {master_socket() or 'not found'}")
        return 0
    watch_inbox()
    app.run("stdio")
    return 0

