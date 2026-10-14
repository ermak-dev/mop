"""mop mcp [--check]: expose pool commands as tools

Started by a claude session. --check shows the local profile and connection
settings; the available tools depend on the session's access.
"""
# Профили решают креды на шине, а не флаги (channel.actor):
#
#   * кред оператора + MOP_PROJECT -> мастер проекта: канал и управление, но
#     только своим срезом пула (`mop master` ставит проект);
#   * кред оператора без проекта -> оператор: то же самое, но по всему пулу;
#   * кред `puppet-<проект>` -> узел: один канал, ростер -- опросом самой шины;
#   * принципал роли `grant-<id>` (#585) -> папет с ролью: инструменты -- по
#     capabilities его бандла, адрес -- имя джоба. Каталог -- не граница
#     прав: её держат сервер и узел по резолверу.
#
# Управление -- сами командлеты: командлет с объявлением `MCP = {...}`
# становится инструментом, своей реализации операций здесь нет.
#
# Так узел физически не может позвать то, чего ему не положено, вместо того
# чтобы не звать по уговору.
#
# Почему не встроенный SendMessage: он видит только этот хост плюс облачные и
# bridge-сессии, а папета пула живут на других узлах. Здесь доставка идёт через
# шину, и хост значения не имеет.
#
# Канал двусторонний. У `send` три адреса, а не два: папет пула, сессия этого
# хоста и мастер — по его инбоксу на шине. Третьего не было, и петля мастера
# стояла на песке: папет отчитывался, `session.find` не находил мастера на своём
# хосте, исключение приезжало модели строкой «Error executing tool send», а
# мастер читал молчание и шёл смотреть чужой экран глазами.
#
# Сокет сессии host-local, поэтому до него дотягивается агент, который на том же
# узле и живёт (mop/node/agent.py). Протокол канала — CHANNEL.md, субъекты шины —
# BUS.md, устройство сервера — MCP.md.
import functools
import inspect
import os
import re
import subprocess
import sys
import threading
from typing import Annotated, Optional


from mcp.server.mcpserver import MCPServer                     # noqa: E402
from mcp.types import ToolAnnotations                          # noqa: E402
from pydantic import Field                                     # noqa: E402

from mop.common import bus, busnames, puppets, state  # noqa: E402
from mop.common.domain import WHO_WAIT, MasterAnswer  # noqa: E402
from mop.client import channel  # noqa: E402
from mop import cli, session  # noqa: E402
from mop.cli import lib                                   # noqa: E402
from mop.cli.core.profile import profile_name               # noqa: E402
from mop.common.render import table                              # noqa: E402

INSTRUCTIONS = (
    "Use this server's `agents` and `send` to find and message pool puppets or "
    "project masters; built-in session tools cannot reach them. Check `agents` "
    "before dispatch: free means the clone has no unsaved work, not merely that "
    "the session is idle. `tail` shows the puppet's screen. Other tools run "
    "the corresponding mop command; a quiet success returns `done`."
)


def instructions(environ):
    """Текст инструкций сервера. Чистая функция (#250).

    Интеграционная ветка человека (#249) приезжает сессии мастера в
    MOP_BRANCH, и одной фразы в скилле оказалось мало: мастер вывел ветку по
    origin/HEAD и истории merge. Инструкции сервера сессия читает при каждом
    старте, до всякого скилла, -- поэтому ветка названа здесь, и названа так,
    чтобы косвенные признаки не голосовали."""
    branch = (environ.get("MOP_BRANCH") or "").strip()
    if not branch:
        return INSTRUCTIONS
    return INSTRUCTIONS + (
        f"\n\nThe integration branch is `{branch}`. Base ticket branches on "
        f"`origin/{branch}`, land into `{branch}`, and name it in dispatch. "
        "Do not substitute the repository's default branch.")


app = MCPServer("mop", instructions=instructions(os.environ))

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
        return channel.actor(bus.config()["user"]) == channel.HUMAN
    except Exception:
        return False


MASTER = is_master()
# Принципал роли -- не мастер (#585): grant-<id> не puppet-<проект>, и
# прежнее `not is_puppet` выдавало ему весь каталог и owner человеком.
KIND = channel.actor(channel.bus_user())
BUNDLE, NO_BUNDLE = channel.own_bundle() if KIND == channel.GRANT else (None, None)
CAPS = BUNDLE.manifest.capabilities if BUNDLE else None


def kind():
    """Вид говорящего этого процесса. MASTER -- прежний шов профиля
    человека/узла; роль -- только по кредам."""
    if KIND == channel.GRANT:
        return channel.GRANT
    return channel.HUMAN if MASTER else channel.LEGACY


def master_id(user):
    """Свой адрес для вестей от агентов и отчётов папетов. Уникален на
    процесс: два терминала, открытые в одном проекте, иначе разбирали бы
    вести друг друга.

    Логин владельца -- первым токеном (#213): человеку можно слушать только
    инбоксы под своим логином. Без кредов шины -- прежний <хост>-<pid>:
    подписываться всё равно нечем."""
    local = f"{os.uname().nodename}-{os.getpid()}"
    return busnames.master_address(user, local) if user else local


def _bus_user():
    try:
        return bus.config().get("user") or None
    except Exception:
        return None


MASTER_ID = master_id(_bus_user())
# Инбокс процесса -- транспортом вестей и reply_to (#585): человек слушает
# мастер-инбокс шины, принципал роли -- свой приватный префикс ответов
# (channel.own_inbox): ACL принципала (#581) подписывает только его.
MY_INBOX = channel.own_inbox(_bus_user(), MASTER_ID)


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


def role_tool(**kw):
    """Инструмент по профилю (channel.tools_for): у мастера -- все ручные,
    кроме dispatch, у узла -- канал, у роли -- по её capabilities. Не
    положенный остаётся обычной питоновской функцией вне списка."""
    allowed = channel.tools_for(kind(), CAPS, {})

    def deco(fn):
        return tool(**kw)(fn) if fn.__name__ in allowed else fn
    return deco


READ_ONLY = ToolAnnotations(readOnlyHint=True)
DESTRUCTIVE = ToolAnnotations(destructiveHint=True)
SLASH_ALLOWED = ("/model", "/clear", "/compact", "/rc", "/status", "Escape")
# Опрос мастеров короче обычного запроса -- domain.WHO_WAIT (#319): одно число
# на agents и сборщик дашборда.


# ─── адресация ───────────────────────────────────────────────────────────
# Маршрут, своя сессия и доставка -- mop/client/channel.py (#148); здесь только
# профиль процесса, от имени которого канал говорит.
def me():
    """Как этот процесс представляется адресату (channel.my_name). Роль --
    именем своего джоба: инбокс процесса MCP -- транспорт, не адрес."""
    if kind() == channel.GRANT:
        return channel.role_name()
    return channel.my_name(MASTER, MASTER_ID)


def node_of(name):
    """Узел папета: у человека и роли -- глаголом alloc сервиса кластера
    (роль -- если он ей выдан), у узла -- опросом агентов."""
    return channel.puppet_node(name, kind() != channel.LEGACY)


def on_inbox(m):
    """Что приехало в инбокс мастера: вести агентов и отчёты папетов.

    Возвращённое становится ответом на запрос — так у папета появляется вердикт
    доставки вместо факта отправки. Агент вести публикует и ответа не ждёт:
    для него не меняется ничего.

    `who` — единственный глагол, который не кладут в сессию: им спрашивают
    общий инбокс проекта, «кто из мастеров жив»."""
    if m.get("verb") == "who":
        if kind() != channel.HUMAN:
            # Инбокс роли -- транспорт вестей, не запись в списке мастеров.
            return {"error": "not a master: a role answers no who-poll"}
        d = channel.my_session() or {}
        # Схема ответа -- одна (#319): domain.MasterAnswer. sessionId -- имя
        # файла сессии: личность сессии уникальна (#421), имя -- нет.
        return MasterAnswer(MASTER_ID, bus.PROJECT, bus.login(), d.get("name"), d.get("cwd"),
                            d.get("sessionId")).to_dict()
    unavailable = {"error": "master session unavailable — choose a live master"}
    sock = channel.master_socket()
    if not sock:
        return unavailable
    # priority по умолчанию later: так кладёт вести агент, и перебивать ход
    # мастера уведомлением о простое незачем. Папет со своим отчётом просит
    # громче — и имеет право, отчёта мастер ждёт.
    try:
        r = session.send(sock, m.get("text") or "",
                         priority=m.get("priority") or "later",
                         from_name=m.get("from") or m.get("node") or "mop")
    except ConnectionError:
        # Сокет может умереть после master_socket; локальный путь в ответ
        # адресату не отдаём, а просьба выбрать живого мастера остаётся.
        return unavailable
    return {"msg_id": r["msg_id"]}


# ─── инструменты: канал ──────────────────────────────────────────────────
@role_tool(annotations=READ_ONLY, description=(
    "List pool puppets, project masters and local sessions. Check before "
    "dispatch: free means no unsaved work in the clone, not just an idle "
    "session. Reply to a master's address only from the masters table "
    "or a received message."))
def agents(project: str = "") -> str:
    if kind() == channel.GRANT:
        # Роль: ростер -- глаголом roster под своим токеном; ни опроса
        # мастеров, ни сокетов хоста -- они не её адресаты.
        return "\n".join(_roster(project, sizes=False))
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


def _roster(project, sizes=True):
    """Ростер мастера: Nomad знает про папета то, чего не знает узел, —
    состояние аллокации, профиль LLM, репозиторий."""
    rows = [("PUPPET", "NODE", "ALLOC", "STATE", "OWNER", "LLM", "REPOSITORY")]
    for r in puppets.puppet_rows(sizes=sizes):
        # Проект -- одно определение (project_of), не подстрока origin: mop
        # сидит внутри rumop (#371). Без origin -- «?», как row_project.
        if project and (puppets.project_of(r.origin) if r.origin else "?") != project:
            continue
        s = r.render()
        rows.append((s["name"], s["node"], s["alloc_status"], s["state"], s["owner"],
                     s["origin"]))
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
            rows.append((name, answer.get("node") or "?", str(state.verdict(facts))))
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
        found = bus.gather("who", timeout=WHO_WAIT,
                           subj=bus.inbox(bus.ALL_MASTERS))
    except bus.BusError as e:
        return ["", f"masters of project {bus.PROJECT}: bus unavailable ({e})"]
    if not found:
        return ["", f"no masters of project {bus.PROJECT} on the bus"]
    rows = [("MASTER (address for send)", "USER", "SESSION", "DIRECTORY")]
    for a in sorted((MasterAnswer.from_dict(d) for d in found), key=lambda a: str(a.master)):
        mine = " (this is me)" if a.master == MASTER_ID else ""
        rows.append((str(a.master), a.user or "-", (a.session or "-") + mine, a.cwd or "-"))
    return ["", "masters of project:", *table(rows)]


@role_tool(description=(
    "Message a puppet, a project master or a local session. Use the address "
    "from agents or a received message. Returns a delivery verdict; "
    "notify_when_idle arranges a later notice. Messaging a puppet claims "
    "ownership; force=true overrides another master's ownership only if "
    "that master agrees or is gone."))
def send(to: str, message: str, priority: str = "next",
         notify_when_idle: bool = False, wait_seconds: int = 0,
         force: bool = False) -> str:
    if priority not in session.PRIORITIES:
        return f"priority must be one of {', '.join(session.PRIORITIES)}"
    if not message.strip():
        return "empty message, nothing to send"
    who = kind()
    kind_, where = channel.route(to, who)
    try:
        if kind_ == "puppet":
            node = node_of(to)
            # Кто пишет (from-name -- адрес для ответа) и владелец задания
            # (#161): мастер называет себя логином, папет и роль -- никем и
            # аренды не берут (channel.send_fields).
            v = channel.send_to_puppet(
                node, to, message, priority, wait_seconds, notify=notify_when_idle,
                via=channel.send_channel(who),
                **channel.send_fields(who, me(), MY_INBOX, force, bus.login()))
        elif kind_ == "local":
            v = channel.send_local(where, message, priority, wait_seconds,
                                   from_name=me())
        else:
            v = channel.send_to_master(to, message, priority, me())
    except bus.BusError as e:
        v = channel.undelivered(kind_, to, e)
    return channel.text(v)


@role_tool(annotations=DESTRUCTIVE, description=(
    "Hand a task to a puppet that receives dispatch: unlike send, the server "
    "may give you its lease and your owner's git identity. Leave `to` empty "
    "to pick a free receiving puppet of this project."))
def dispatch(to: str, message: str, priority: str = "next") -> str:
    if kind() != channel.GRANT or not (CAPS and CAPS.dispatch):
        return "dispatch: not permitted for this role — use send"
    if priority not in session.PRIORITIES:
        return f"priority must be one of {', '.join(session.PRIORITIES)}"
    if not message.strip():
        return "empty message, nothing to send"
    if not to:
        to = channel.pick(channel.candidates(puppets.puppet_rows(sizes=False)),
                          bus.PROJECT, None)
        if not to:
            return ("dispatch: no puppet of this project receives dispatch with a "
                    "free clone and no lease — see agents")
    if not channel.is_puppet(to):
        return f"dispatch: {to} is not a puppet's job name"
    try:
        v = channel.dispatch_to_puppet(node_of(to), to, message, priority,
                                       from_name=me())
    except bus.BusError as e:
        v = channel.undelivered("puppet", to, e)
    return channel.text(v)


@role_tool(annotations=READ_ONLY, description=(
    "Read recent output from a puppet's screen, including dialogs and "
    "errors missing from its state."))
def tail(name: str, lines: int = 40, grep: str = "") -> str:
    try:
        buf = puppets.pane_lines(node_of(name), name)
    except (LookupError, RuntimeError) as e:
        # Сессии нет (#333) -- тот же текст, что у `mop tail`: stderr
        # аллокации. Спросить сервис кластера может только мастер.
        if not MASTER:
            raise
        return "\n".join(lib.stderr_text(name, e, puppets.alloc_stderr(name, lines)))
    if grep:
        buf = [l for l in buf if grep.lower() in l.lower()]
    return "\n".join(buf[-max(1, lines):]) or "(empty)"


# ─── инструменты: управление ─────────────────────────────────────────────
@role_tool(annotations=DESTRUCTIVE, description=(
    "Type a permitted slash command or Escape in a puppet's session. "
    f"Allowed: {', '.join(SLASH_ALLOWED)}. force=true overrides another "
    "master's ownership only if that master agrees or is gone."))
def slash(name: str, command: str, force: bool = False) -> str:
    if not command.split()[0:1] or command.split()[0] not in SLASH_ALLOWED:
        return f"only allowed: {', '.join(SLASH_ALLOWED)}"
    node = node_of(name)
    if command.startswith("/model "):
        puppets.switch_model(node, name, command.split(None, 1)[1], force)
        return f"{name}: {command}"
    return f"{name}: {command}\n{puppets.type_command(node, name, command, force)[-1500:]}"


# ─── инструменты: командлеты (#160) ──────────────────────────────────────
# Управление пулом -- не вторая реализация, а сами командлеты. Командлет,
# объявивший `MCP = {...}` (mop/cli/__init__.py), становится инструментом:
# описание -- его докстринг, вызов -- `mop <команда>` подпроцессом из
# каталога сессии мастера, ответ -- его вывод как есть. Своя копия add и
# build здесь уже разошлась однажды с командлетами (workspace, сборщик);
# копии нет -- расходиться нечему.
COMMAND_TIMEOUT = 600
TYPES = {"string": str, "integer": int, "boolean": bool}
HINTS = {"readonly": READ_ONLY, "destructive": DESTRUCTIVE}


def run_command(words, argv):
    """`mop <слова> <argv>` из каталога сессии -> ответ модели.

    Каталог -- сессии, а не процесса: сервер запускают откуда угодно, а
    origin и workspace командлет берёт из рабочей копии, в которой стоит
    мастер. Вывод отдаётся как есть, без разбора: он английский и читается
    моделью (CLAUDE.md), а цвета терминала снимаются."""
    cwd = (channel.my_session() or {}).get("cwd") or os.getcwd()
    try:
        r = subprocess.run([*lib.self_argv(), *words, *argv], cwd=cwd, stdin=subprocess.DEVNULL,
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
        sock = channel.master_socket()
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
        t = list[str] if a.get("repeatable") else TYPES[a["type"]]
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
    """Объявленные командлеты -- инструменты. У мастера -- все, у роли --
    назвавшие свои глаголы в пределах её capabilities (#585), у узла --
    никаких: управление ему не положено, и его креды в сервис кластера не
    пишут."""
    if kind() == channel.LEGACY:
        return
    found = {}
    for name, words, path in cli.tool_commands(cli.scan(), cli.verbs()):
        decl = cli.declared(path)
        if decl is not None:
            found[name] = (words, path, decl)
    allowed = channel.tools_for(kind(), CAPS, {n: d for n, (_, _, d) in found.items()})
    for name, (words, path, decl) in found.items():
        if name in allowed:
            command_tool(name, words, path, decl)


command_tools()


REFUSED = re.compile(r'permissions violation for subscription to "([^"]+)"')
_refused_told = set()


def refused_inbox(error, subjects):
    """Текст асинхронной ошибки шины -> субъект из subjects, в подписке на
    который шина отказала, либо None."""
    m = REFUSED.search(error or "")
    return m.group(1) if m and m.group(1) in subjects else None


def on_bus_error(error):
    """Шина отказала в подписке на свой инбокс (#213) -- громко: соединение
    при этом живо, а вести и отчёты папетов молча не доходят. Строка -- в
    сессию и в stderr MCP-сервера: если сессия push не примет, след остаётся
    в логе. Один раз на субъект."""
    subj = refused_inbox(error, (MY_INBOX, bus.inbox(bus.ALL_MASTERS)))
    if subj is None or subj in _refused_told:
        return
    _refused_told.add(subj)
    line = (f"mop: the bus refused this master's inbox {subj} -- notes from agents "
            f"and puppet reports will not arrive; restart the mop MCP server (/mcp)")
    print(line, file=sys.stderr, flush=True)
    sock = channel.master_socket()
    if sock:
        try:
            session.send(sock, line, priority="now", from_name="mop")
        except Exception:
            pass


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
    who = kind()
    if who == channel.LEGACY or not channel.master_socket():
        return
    bus.ERROR_LISTENERS.append(on_bus_error)
    try:
        bus.subscribe(MY_INBOX, on_inbox)
        # Роль слушает только свой инбокс -- транспорт вестей (#585): в
        # опросе мастеров её нет, и процессов MCP в теле сколько угодно.
        if who == channel.HUMAN:
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
            where = f"none ({e})"
        if kind() == channel.GRANT:
            who = f"role {BUNDLE.role}" if BUNDLE else f"role without a bundle ({NO_BUNDLE})"
        else:
            who = profile_name(MASTER, bus.PROJECT)
        # Ни адреса, ни инбокса (#226): --check -- отдельный процесс, и его
        # pid не pid живого сервера сессии. Напечатанный адрес не слушал бы
        # никто, а отданный папету увёл бы ответ в пустоту. Адрес мастера --
        # только из таблицы мастеров в agents или из конверта письма.
        print(f"mop mcp: profile {who}, "
              f"{len(app._tool_manager.list_tools())} tools, bus {where}, "
              f"session: {channel.master_socket() or 'not found'}")
        # Мастер без проекта -- оператор admin-среза (#481): его who-ответ
        # уходит в admin-инбоксы, опрос проекта его не находит, и живая
        # сессия невидима в «Мастерах» дашборда. Сессию в каталоге проекта
        # так и не нашли -- а молчание --check выглядело как «всё в порядке».
        # bus.PROJECT пустым не бывает: без MOP_PROJECT он уже равен ADMIN.
        if MASTER and bus.PROJECT == bus.ADMIN:
            print("warning: no MOP_PROJECT -- you answer in the admin slice and "
                  "will not appear in a project's masters; run mop master <project> "
                  "or export MOP_PROJECT")
        return 0
    watch_inbox()
    app.run("stdio")
    return 0

