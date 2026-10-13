#!/usr/bin/env python3
"""Канал сообщений без пула: python3 tests/channel.py

Две части. Первая -- характеристика: оба фронтенда (`mop send` и инструмент
`send` в MCP) прогоняются на заглушках шины и сокета, и их тексты и запросы
агенту приколоты байт в байт такими, какими они были до #148. Вторая --
сам канал (mop/client/channel.py): выбор пути по адресу, потолок ожидания, вердикт
-> текст.

HYPOTHESIS (#148): маршрутизация трёх путей и поиск своей сессии жили во
фронтенде MCP, а MAX_WAIT, обрезка ожидания и тексты «NOT DELIVERED — » и
«inbox not listening — session is dead» -- ещё и копией в cli/core/send.py.
SOLUTION: mop/client/channel.py -- send_to_puppet, send_local, send_to_master,
MAX_WAIT, своя сессия; возвращает вердикт (данные), не печатает. Фронтенды
только рисуют.
"""
import contextlib
import io
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, Msg, patched, patched_env, restored, run_command  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
os.environ.setdefault("MOP_SERVER_LAN", "127.0.0.1")

from mop.common import bus, puppets  # noqa: E402
from mop import session  # noqa: E402
from mop.cli import lib  # noqa: E402
from mop.cli.core import send as cli_send  # noqa: E402
from mop.cli.service import mcp  # noqa: E402

PUPPET = "pu-mop-9"
NOTE = "was led by olga; now yours"
OWNED = "pu-mop-9 is led by olga (work in its clone) — force=true takes it over"
SESS = {"name": "s1", "pid": 123, "messagingSocketPath": "/run/user/1000/cc-socks/123.sock",
        "cwd": "/w"}


class Stubs:
    """Заглушки шины, сокета и ростера на время блока with. Что спросили --
    в calls, что ответить -- в answers по пути."""

    def __init__(self):
        self.calls = []
        self.answers = {}
        self.alive = True
        self.stack = contextlib.ExitStack()

    def put(self, mod, name, fn):
        """Подменить ещё один атрибут до конца блока."""
        self.stack.enter_context(patched(mod, **{name: fn}))

    def __enter__(self):
        def request(node, verb, **kw):
            self.calls.append(("request", node, verb, kw))
            return self._answer("request")

        def ask(master_id, verb, **kw):
            self.calls.append(("ask", master_id, verb, kw))
            return self._answer("ask")

        def send(sock, body, **kw):
            self.calls.append(("session.send", sock, body, kw))
            return self._answer("session.send")

        def find(target):
            if target == SESS["name"] or target == str(SESS["pid"]):
                return SESS
            if target == "twins":
                raise session.Ambiguous("'twins' matches several sessions: twins[1], twins[2] — specify pid")
            raise LookupError(f"session not found: {target}")

        self.put(bus, "request", request)
        self.put(bus, "ask", ask)
        self.put(bus, "login", lambda: "anton")
        self.put(session, "send", send)
        self.put(session, "find", find)
        self.put(session, "socket_alive", lambda path: self.alive)
        self.put(session, "sessions", lambda: [SESS])
        self.put(puppets, "running_alloc",
                 lambda name: {"NodeName": "n1", "ClientStatus": "running"})
        self.put(lib, "guard", lambda name: None)
        self.put(mcp, "MASTER", True)
        self.put(mcp, "MASTER_ID", "host-77")
        self.put(mcp, "MY_INBOX", "mop.mop.master.host-77.inbox")
        return self

    def _answer(self, path):
        a = self.answers.get(path, {"msg_id": "m1"})
        if isinstance(a, Exception):
            raise a
        return a

    def __exit__(self, *exc):
        self.stack.close()


def cli(argv):
    """`mop send` -> (код, stdout, stderr). sys.exit(строка) -- как у
    интерпретатора: строка в stderr, код 1."""
    out, err, code = run_command(cli_send.main, argv)
    if isinstance(code, str):
        err, code = err + code + "\n", 1
    return code or 0, out, err


def check_bus_envelope_264(c):
    """HYPOTHESIS (#264): конверт запроса `{"verb": ..., **поля}` собирался
    в bus.py четырежды (у ask_once -- без ensure_ascii=False), переход на
    прежний субъект при NoRespondersError (#207) был записан трижды (ask_once,
    _ask, _one), а request_many/request_stream заставляли девять мест
    собирать {"verb": ...} руками; результаты write и bus.failure разбирали
    ответ узла одной и той же тройной проверкой каждый сам.
    SOLUTION: один конверт (bus.envelope), одна корутина запроса с переходом
    (bus.arequest) на всех путях; request_many(verb, узлы, **поля) и
    request_stream(verb, {ключ: (узел, поля)}, **поля) -- той же формы, что
    request; разбор ответа -- bus.verdict. STATUS: FIXED — see #264

    Соединение -- заглушка: субъект с логином отвечает NoRespondersError,
    прежний -- эхом того, что пришло."""
    import json
    from mop.server import proxykey
    failed_before = c.failed

    class Conn:
        def __init__(self):
            self.seen = []

        async def request(self, subj, data, timeout=None):
            self.seen.append((subj, data))
            if busnames.caller(subj):
                raise bus.NoRespondersError()
            return Msg(data=json.dumps({"subj": subj, "got": json.loads(data.decode())},
                                  ensure_ascii=False).encode())

        async def close(self):
            pass

    from mop.common import busnames
    word = "Ωмега"
    raw = word.encode()

    def utf8(conn):
        return all(raw in d and b"\\u" not in d for _, d in conn.seen)

    def fell_back(conn, nodes):
        subs = [s for s, _ in conn.seen]
        return all(busnames.node("mop", n, "rpc", login="alice") in subs
                   and busnames.node("mop", n, "rpc") in subs for n in nodes)
    with restored(bus, "connect", "login", "_open"):
        bus.login = lambda: "alice"
        # request
        conn = Conn()
        bus.connect = lambda *a, **k: conn
        try:
            got = bus.request("n1", "state", project="mop", name=word)
            ok = got.get("got") == {"verb": "state", "name": word} and \
                fell_back(conn, ["n1"]) and utf8(conn)
        except Exception as e:
            ok, got = False, e
        c.check("request: fallback subject and UTF-8 envelope", ok, f"got {got!r}, {conn.seen}")
        # request_many: общий глагол и общие поля
        conn = Conn()
        try:
            got = bus.request_many("states", ["n1", "n2"], project="mop", names=[word])
            ok = {n: a.get("got") for n, a in got.items()} == \
                {n: {"verb": "states", "names": [word]} for n in ("n1", "n2")} and \
                fell_back(conn, ["n1", "n2"]) and utf8(conn)
        except Exception as e:
            ok, got = False, e
        c.check("request_many(verb, nodes, **fields)", ok, f"got {got!r}")
        # request_many: поля у каждого узла свои, общие -- поверх
        conn = Conn()
        try:
            got = bus.request_many("states", {"n1": {"names": ["a"]}, "n2": {"names": [word]}},
                                   project="mop", why=word)
            ok = {n: a.get("got") for n, a in got.items()} == {
                "n1": {"verb": "states", "why": word, "names": ["a"]},
                "n2": {"verb": "states", "why": word, "names": [word]}}
        except Exception as e:
            ok, got = False, e
        c.check("request_many per-node fields", ok, f"got {got!r}")
        # request_stream
        conn = Conn()
        try:
            got = dict(bus.request_stream("sizes", {"k": ("n1", {"names": [word]})},
                                          project="mop"))
            ok = got["k"].get("got") == {"verb": "sizes", "names": [word]} and \
                fell_back(conn, ["n1"]) and utf8(conn)
        except Exception as e:
            ok, got = False, e
        c.check("request_stream(verb, {key: (node, fields)})", ok, f"got {got!r}")
        # ask_once -- своё соединение, тот же конверт и переход
        conn = Conn()

        async def opened(**kw):
            return conn
        bus._open = opened
        try:
            got = bus.ask_once({"user": "alice", "password": "pw", "url": "nats://127.0.0.1:4222"},
                               busnames.cluster("mop", login="alice"),
                               "ping", who=word)
            ok = got.get("got") == {"verb": "ping", "who": word} and \
                [s for s, _ in conn.seen] == [busnames.cluster("mop", login="alice"),
                                               busnames.cluster("mop")] and utf8(conn)
        except Exception as e:
            ok, got = False, e
        c.check("ask_once: fallback subject and UTF-8 envelope", ok, f"got {got!r}, {conn.seen}")

    # Конверт -- в одном месте: json.dumps в bus.py один, {"verb" -- нигде
    # в mop/, кроме него.
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    src = open(os.path.join(root, "mop", "common", "bus.py")).read()
    c.expect("bus.py must encode in one place (json.dumps count)", src.count("json.dumps("), 1)
    by_hand = []
    for d, _, files in os.walk(os.path.join(root, "mop")):
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(d, f)
                for n, line in enumerate(open(p), 1):
                    if '{"verb":' in line and "mop/common/bus.py" not in p:
                        by_hand.append(f"{os.path.relpath(p, root)}:{n}")
    c.check("no envelopes built by hand", not by_hand, repr(by_hand))

    # Разбор ответа узла -- одна функция на bus.failure и bus.results_from
    # (до #315 -- keys.results_from),
    # и тексты у обоих прежние.
    cases = {"n1": RuntimeError("down"), "n2": None, "n3": {"error": "nope"}, "n4": {"ok": 1}}
    want_keys = {"n1": "NOT REACHED: down", "n2": "NOT REACHED: no answer",
                 "n3": "FAILED: nope", "n4": "OK"}
    want_failure = {"n1": "down", "n2": "no response", "n3": "nope", "n4": None}
    c.expect("bus.results_from unchanged", bus.results_from(list(cases), cases), want_keys)
    c.expect("bus.failure unchanged", {n: bus.failure(a) for n, a in cases.items()}, want_failure)
    verdict = getattr(bus, "verdict", None)
    if c.check("bus.verdict exists: the three-way check lives in one place", verdict is not None):
        import inspect
        fn = getattr(bus, "results_from", None)
        c.check("bus.results_from must classify through bus.verdict",
                fn is not None and "verdict(" in inspect.getsource(fn))
    return c.failed == failed_before


def check_stale_permissions_error_369(c):
    """HYPOTHESIS (#369): bus._last_error пишет только _on_error и не
    сбрасывает никто, а _silence читает его при каждом таймауте. В
    долгоживущем процессе (MCP мастера, mop-web) один давний отказ прав
    выдаётся за причину каждого следующего таймаута любого узла, и диагноз
    ведёт чинить права вместо молчащего агента.
    SOLUTION: ошибка хранится вместе с моментом (bus._clock, монотонные
    часы); таймаут запроса и отказ подключения объясняет только ошибка,
    пришедшая после начала этого запроса или подключения. Тот же
    закон -- у _one (request_many/request_stream). STATUS: FIXED — see #369

    Часы и соединение -- заглушки: время двигает сам тест, без sleep."""
    import asyncio
    failed_before = c.failed
    now = [100.0]
    violation = "nats: permissions violation for publish to \"mop.mop.node.n1.rpc\""

    class Conn:
        def __init__(self, during=False):
            self.during = during

        async def request(self, subj, data, timeout=None):
            if self.during:
                now[0] += 1
                await bus._on_error(Exception(violation))
            raise asyncio.TimeoutError()

    def timeout_text(conn):
        bus.connect = lambda *a, **k: conn
        try:
            bus.request("n1", "state", timeout=3, project="mop")
        except bus.BusError as e:
            return str(e)
        return "no BusError"

    with restored(bus, "connect", "login", "_clock", "_last_error", "ERROR_LISTENERS"):
        bus.login = lambda: None
        bus._clock = lambda: now[0]
        bus.ERROR_LISTENERS = []
        # отказ прав давний: пришёл до начала запроса
        asyncio.run(bus._on_error(Exception(violation)))
        now[0] = 200.0
        c.expect("#369 an old permissions error does not explain a later timeout",
                 timeout_text(Conn()), "node agent n1 did not answer in 3s")
        # отказ прав пришёл во время запроса -- он и есть причина
        got = timeout_text(Conn(during=True))
        c.check("#369 a permissions error during the request explains its timeout",
                got.startswith("bus did not let the request through — node agent n1")
                and "permissions violation" in got, got)
        # тот же закон у request_many: его узлы спрашивает _one, а не _ask
        now[0] = 300.0
        bus.connect = lambda *a, **k: Conn()
        got = bus.request_many("state", ["n1"], timeout=3, project="mop")["n1"]
        c.expect("#369 request_many: an old permissions error does not explain a timeout",
                 str(got), "node agent n1 did not answer in 3s")
    return c.failed == failed_before


def check_agents_project_371(c):
    """HYPOTHESIS (#371): _roster фильтрует по подстроке отрендеренного
    origin (`project not in s["origin"]`): проект mop -- подстрока
    .../rumop.git, и agents(project="mop") показывает папетов rumop.
    SOLUTION: сравнение по значению PuppetRow -- puppets.project_of(origin),
    строка без origin -- «?», как row_project дашборда. Одно определение
    проекта (CLAUDE.md). STATUS: FIXED — see #371"""
    from mop.common.state import PuppetRow
    failed_before = c.failed

    def row(name, origin):
        return PuppetRow(name=name, node="n1", alloc_status="running", state="free",
                         kind="free", owner=None, origin=origin)
    rows = [row("pu-mop-1", "git@git.example:ermak/mop.git"),
            row("pu-rumop-1", "git@git.example:ermak/rumop.git"),
            row("pu-none-1", None)]
    with patched(puppets, puppet_rows=lambda *a, **k: rows):
        got = "\n".join(mcp._roster("mop"))
        c.check("#371 agents(project=mop) shows the mop puppet", "pu-mop-1" in got, got)
        c.check("#371 agents(project=mop) hides the rumop puppet", "pu-rumop-1" not in got, got)
        c.check("#371 agents(project=mop) hides a puppet without origin",
                "pu-none-1" not in got, got)
        got = "\n".join(mcp._roster(""))
        c.check("#371 no project: every puppet",
                all(n in got for n in ("pu-mop-1", "pu-rumop-1", "pu-none-1")), got)
    return c.failed == failed_before


GRANT = "grant-" + "a" * 32
ROLES = {
    # Пресеты docs/SOUL.md: мастер-роль диспетчерит, сама не принимает;
    # рабочий -- наоборот.
    "master": {"soul": "master", "version": 1,
               "runtime": {"driver": "isolating", "receives_dispatch": False},
               "capabilities": {
                   "node_verbs": ["send", "tail", "state", "states", "ping",
                                  "local", "type", "wipe", "usage", "clone"],
                   "cluster_verbs": ["ping", "roster", "pool", "add", "update",
                                     "restart", "stop", "delete", "alloc",
                                     "stderr", "spec", "bootstrap_result",
                                     "landing"],
                   "dispatch": True, "spawn": ["puppet", "builder"]},
               "prompt": "soul.md"},
    "puppet": {"soul": "puppet", "version": 1,
               "runtime": {"driver": "any", "receives_dispatch": True},
               "capabilities": {"node_verbs": ["send", "tail", "state", "states",
                                               "ping", "local"],
                                "cluster_verbs": [], "dispatch": False, "spawn": []},
               "prompt": "soul.md"},
}


def caps_of(role, **over):
    from mop.common import soul
    doc = dict(ROLES[role])
    doc["capabilities"] = dict(doc["capabilities"], **over)
    return soul.from_manifest(doc, b"prompt").manifest.capabilities


def check_role_channel_585(c):
    """HYPOTHESIS (#585): профиль MCP решает вопрос «мастер ли мы» одной
    проверкой `not is_puppet(user)`, и делегированный принципал роли
    (grant-<id>, #579) выходит мастером: весь каталог командлетов, owner
    человеком в каждом send, подписка на чужие master-инбоксы. А легаси-
    папет (puppet-<проект>) шлёт send в node.<n>.rpc, куда его креды не
    пишут (natsconf.puppet_permissions -- только .msg): живой peer-send
    pu2/pu4 упал permissions violation.
    SOLUTION: channel.actor -- три вида говорящего (human/legacy/grant);
    легаси -- канал msg; grant -- адрес = имя джоба, без owner/force/
    reply_to, свой узел из окружения тела, адресат -- verb peers своего
    узла, каталог инструментов -- из capabilities бандла; dispatch --
    отдельная операция в своём канале; autopick -- только
    receives_dispatch + свободный клон + проект + аренда."""
    from mop.client import channel
    # Сначала -- поведение сегодняшнего маршрута, без нового API: красный
    # обязан быть про маршрут, а не про отсутствующее имя.
    with patched(bus, config=lambda *a: {"user": GRANT}):
        c.expect("#585 a grant is not a master profile", mcp.is_master(), False)

    # Легаси-папет: send -- канал msg, остальное прежнее.
    with Stubs() as s:
        s.put(mcp, "MASTER", False)
        s.put(mcp, "KIND", "legacy")
        s.put(bus, "gather", lambda verb, **kw: [{"node": "n1", "puppets": {PUPPET: {}}}])
        s.put(bus, "login", lambda: None)
        s.answers["request"] = {"msg_id": "m1"}
        with patched_env(CLAUDE_CODE_MESSAGING_SOCKET=SESS["messagingSocketPath"]):
            s.put(session, "sessions", lambda: [dict(SESS, cwd="/p/pu-mop-4")])
            got = mcp.send(PUPPET, "hi")
        c.expect("#585 legacy send delivered", got, "pu-mop-9: delivered (msg_id=m1)")
        kw = s.calls[-1][3]
        c.expect("#585 legacy send goes over msg, which its creds may publish",
                 kw.get("channel"), "msg")
        c.expect("#585 legacy send carries no owner", kw.get("owner"), None)
        c.expect("#585 legacy send names its clone", kw.get("from_name"), "pu-mop-4")
    # Человек -- байт в байт прежний запрос (характеристика выше): без channel.

    # Грант: адрес -- имя джоба, ни владельца, ни force; reply_to -- только
    # транспорт вестей в инбокс процесса (решение мастера по C5). Узел
    # адресата -- глаголом alloc кластера под своим токеном (C4: roster/alloc
    # выдаются ролям декларативно), не broadcast.
    env = {"MOP_PUPPET": "pu-mop-3"}
    with Stubs() as s, patched_env(**env):
        # Профиль -- тот, что модуль вывел бы из кредов гранта, а не
        # подставленный руками.
        with patched(bus, config=lambda *a: {"user": GRANT}):
            s.put(mcp, "MASTER", mcp.is_master())
        s.put(mcp, "KIND", "grant")
        s.put(mcp, "CAPS", caps_of("puppet"))
        s.put(mcp, "MASTER_ID", f"{GRANT}.host-77")
        s.put(mcp, "MY_INBOX", f"mop.mop.master.{GRANT}.host-77.inbox")
        s.put(bus, "login", lambda: GRANT)
        s.put(bus, "gather", lambda *a, **kw: c.fail("#585 grant never broadcasts", a))

        got = mcp.send(PUPPET, "hi", force=True)
        c.expect("#585 grant send delivered", got, "pu-mop-9: delivered (msg_id=m1)")
        c.expect("#585 grant reaches the target's node",
                 [x[1:3] for x in s.calls if x[0] == "request"], [("n1", "send")])
        kw = s.calls[-1][3]
        c.expect("#585 grant send is plain rpc", kw.get("channel", "rpc"), "rpc")
        c.expect("#585 grant names itself by its job",
                 kw.get("from_name"), "pu-mop-3")
        c.expect("#585 no false owner binding", "owner" in kw, False)
        c.expect("#585 plain send takes no lease: force does not travel",
                 "force" in kw, False)
        c.expect("#585 the process inbox is transport only (reply_to), not the sender",
                 (kw.get("reply_to"), kw.get("from_name")),
                 (f"mop.mop.master.{GRANT}.host-77.inbox", "pu-mop-3"))
        c.expect("#585 me() of a grant", mcp.me(), "pu-mop-3")
    return True


def check_role_api_585(c):
    """#585, вторая часть: вид говорящего, адреса, подписки, dispatch,
    каталог инструментов, autopick -- новое API канала."""
    from mop.client import channel
    try:
        actor = channel.actor
    except AttributeError as e:
        c.fail("#585 channel.actor exists", str(e))
        return False
    c.expect("#585 grant is its own actor", actor(GRANT), channel.GRANT)
    c.expect("#585 legacy puppet creds", actor("puppet-mop"), channel.LEGACY)
    c.expect("#585 no creds read as legacy", actor(None), channel.LEGACY)
    c.expect("#585 a person", actor("anton"), channel.HUMAN)
    with patched(bus, config=lambda *a: {"user": GRANT}):
        c.expect("#585 a grant is no human owner", channel.human_login(), None)
    with patched(bus, config=lambda *a: {"user": "anton"}):
        c.expect("#585 a person is still owner", channel.human_login(), "anton")
    env = {"MOP_PUPPET": "pu-mop-3"}
    with Stubs() as s, patched_env(**env):
        s.put(mcp, "MASTER", False)
        s.put(mcp, "KIND", channel.GRANT)
        s.put(mcp, "CAPS", caps_of("puppet"))
        s.put(mcp, "MASTER_ID", f"{GRANT}.host-77")
        s.put(mcp, "MY_INBOX", f"mop.mop.master.{GRANT}.host-77.inbox")
        s.put(bus, "login", lambda: GRANT)
        s.put(bus, "gather", lambda *a, **kw: c.fail("#585 grant never broadcasts", a))

        # Адреса: старые адреса мастеров-людей работают в переходе, имя
        # роли адресом не является, сокеты соседей по хосту -- не путь роли.
        old = "anton%2Eermak.wate.lan-7"
        c.expect("#585 grant may answer a human master's address",
                 channel.route(old, channel.GRANT), ("master", old))
        c.expect("#585 legacy master address without login",
                 channel.route("host-1", channel.GRANT), ("master", "host-1"))
        for label in ("builder", "s1"):
            try:
                got = channel.route(label, channel.GRANT)
                c.fail(f"#585 {label!r} is not an address of a role", got)
            except LookupError as e:
                c.check("#585 the refusal says how to address", "job name" in str(e), str(e))
        c.expect("#585 a human still routes any name to a master inbox",
                 channel.route("builder"), ("master", "builder"))
        c.expect("#585 a human still reaches a session on its host",
                 channel.route("s1"), ("local", SESS))

        # Многопроцессный MCP одного тела: свой инбокс -- транспорт вестей,
        # опрос мастеров (who) роль не слушает и мастером не считается.
        subs = []
        s.put(bus, "subscribe", lambda subj, h: subs.append(subj))
        s.put(bus, "ERROR_LISTENERS", [])
        with patched_env(CLAUDE_CODE_MESSAGING_SOCKET=SESS["messagingSocketPath"]):
            mcp.watch_inbox()
            c.expect("#585 a role answers no who-poll", "error" in mcp.on_inbox({"verb": "who"}), True)
        c.expect("#585 a role MCP subscribes its own inbox only",
                 subs, [f"mop.mop.master.{GRANT}.host-77.inbox"])

        # dispatch: без права -- отказ; с правом -- свой канал, без owner.
        c.check("#585 dispatch refused without the capability",
                "not permitted" in mcp.dispatch(PUPPET, "task"))
        s.put(mcp, "CAPS", caps_of("master"))
        got = mcp.dispatch(PUPPET, "task")
        c.expect("#585 dispatch delivered", got, "pu-mop-9: delivered (msg_id=m1)")
        last = s.calls[-1]
        c.expect("#585 dispatch is its own operation", (last[1], last[2], last[3].get("channel")),
                 ("n1", "dispatch", "dispatch"))
        c.expect("#585 dispatch body carries no authority",
                 [k for k in ("owner", "force", "role", "_caller") if k in last[3]], [])
        c.expect("#585 dispatch names the sender by its job", last[3].get("from_name"), "pu-mop-3")
        # autopick: мастер-роль чистая, но не принимает -- выбран рабочий.
        from mop.common.state import PuppetRow

        def row(name, kind, receives, owner=None):
            r = PuppetRow(name, "n1", "running", kind, kind, owner,
                          origin="git@h:ermak/mop.git", soul="x")
            object.__setattr__(r, "receives_dispatch", receives) if receives is not None else None
            return r
        s.put(puppets, "puppet_rows", lambda sizes=True: [
            row("pu-mop-1", "free", False), row("pu-mop-2", "busy", True),
            row(PUPPET, "free", True), row("pu-mop-8", "free", None)])
        s.put(bus, "PROJECT", "mop")
        got = mcp.dispatch("", "task")
        c.expect("#585 autopick dispatches to the receiving free puppet",
                 (got, s.calls[-1][3].get("name")), ("pu-mop-9: delivered (msg_id=m1)", PUPPET))
        s.put(puppets, "puppet_rows", lambda sizes=True: [row("pu-mop-1", "free", False)])
        c.check("#585 autopick with nobody eligible refuses with a reason",
                "no puppet" in mcp.dispatch("", "task"))
        s.put(mcp, "KIND", channel.HUMAN)
        s.put(mcp, "MASTER", True)
        c.check("#585 a human has no dispatch: send keeps its lease semantics",
                "not permitted" in mcp.dispatch(PUPPET, "task"))

    # Каталог инструментов: из capabilities и объявлений командлетов.
    decls = {"wipe": {"node_verbs": ["wipe"]}, "add": {"cluster_verbs": ["add"]},
             "drain": {"cluster_verbs": ["drain"]}, "models": {}}
    hand = channel.HAND_TOOLS
    c.expect("#585 a human keeps every tool",
             channel.tools_for(channel.HUMAN, None, decls), (set(hand) | set(decls)) - {"dispatch"})
    c.expect("#585 legacy puppet: the channel only",
             channel.tools_for(channel.LEGACY, None, decls), {"agents", "send", "tail"})
    decls["profile"] = {"cluster_verbs": []}
    c.expect("#585 puppet role as preset today: no discovery without roster",
             channel.tools_for(channel.GRANT, caps_of("puppet"), decls),
             {"send", "tail", "profile"})
    c.expect("#585 puppet role with declared roster/alloc: discovery",
             channel.tools_for(channel.GRANT, caps_of("puppet", cluster_verbs=["roster", "alloc"]),
                               decls),
             {"agents", "send", "tail", "profile"})
    c.expect("#585 master role: its verbs, dispatch, declared commandlets",
             channel.tools_for(channel.GRANT, caps_of("master"), decls),
             {"agents", "send", "tail", "slash", "dispatch", "wipe", "add", "profile"})
    c.expect("#585 empty node_verbs: no channel tools",
             channel.tools_for(channel.GRANT, caps_of("puppet", node_verbs=[]), decls),
             {"profile"})
    c.expect("#585 a grant without a bundle fails closed",
             channel.tools_for(channel.GRANT, None, decls), set())

    # autopick: чистая функция.
    rows = [
        {"name": "pu-mop-1", "project": "mop", "receives_dispatch": False, "free": True, "owner": None},
        {"name": "pu-mop-2", "project": "mop", "receives_dispatch": True, "free": False, "owner": None},
        {"name": "pu-mop-3", "project": "mop", "receives_dispatch": True, "free": True, "owner": "olga"},
        {"name": "pu-x-1", "project": "x", "receives_dispatch": True, "free": True, "owner": None},
        {"name": "pu-mop-5", "project": "mop", "receives_dispatch": True, "free": True, "owner": "anton"},
        {"name": "pu-mop-4", "project": "mop", "receives_dispatch": True, "free": True, "owner": None},
    ]
    c.expect("#585 autopick: eligible, free, same project, lease allows",
             channel.pick(rows, "mop", "anton"), "pu-mop-4")
    c.expect("#585 autopick: own lease is fine",
             channel.pick([r for r in rows if r["name"] != "pu-mop-4"], "mop", "anton"), "pu-mop-5")
    c.expect("#585 autopick: nobody eligible", channel.pick(rows[:4], "mop", "anton"), None)
    return True


def main():
    c = Checks()

    # ─── характеристика: MCP ──────────────────────────────────────────────
    with Stubs() as s:
        mcp_request = {"name": PUPPET, "message": "hi", "priority": "next", "wait": 0,
                       "notify": False, "from_name": "host-77", "owner": "anton",
                       "force": False, "reply_to": "mop.mop.master.host-77.inbox",
                       "timeout": bus.TIMEOUT}
        s.answers["request"] = {"msg_id": "m1"}
        c.expect("mcp puppet delivered", mcp.send(PUPPET, "hi"), "pu-mop-9: delivered (msg_id=m1)")
        c.expect("mcp puppet request", s.calls[-1], ("request", "n1", "send", mcp_request))

        s.answers["request"] = {"msg_id": "m1", "owner_note": NOTE}
        c.expect("mcp owner note", mcp.send(PUPPET, "hi"),
                 f"pu-mop-9: delivered (msg_id=m1); {NOTE}")

        s.answers["request"] = {"error": OWNED}
        c.expect("mcp owner refusal", mcp.send(PUPPET, "hi"), f"pu-mop-9: NOT DELIVERED — {OWNED}")
        s.answers["request"] = {"msg_id": "m1"}
        mcp.send(PUPPET, "hi", force=True)
        c.expect("mcp force travels", s.calls[-1][3]["force"], True)

        # #574 ревью 3 п.5: тело молчит -- {"error", "timeout"}: доставка
        # неизвестна, не отказана. Отдельная строка, не «NOT DELIVERED»:
        # повторять высланное можно, звать чужую аренду заново -- нет.
        s.answers["request"] = {"error": "pu-mop-9: the body did not answer in 20s",
                                "timeout": True}
        c.expect("mcp indeterminate delivery",
                 mcp.send(PUPPET, "hi"),
                 "pu-mop-9: DELIVERY UNKNOWN — pu-mop-9: the body did not answer in 20s")
        s.answers["request"] = {"msg_id": "m1"}

        s.answers["request"] = bus.BusError("node agent n1 did not answer in 20s")
        c.expect("mcp bus error", mcp.send(PUPPET, "hi"),
                 "pu-mop-9: NOT DELIVERED — node agent n1 did not answer in 20s")

        s.answers["request"] = {"msg_id": "m1", "idle": "went idle after 12s"}
        c.expect("mcp wait idle", mcp.send(PUPPET, "hi", wait_seconds=30),
                 "pu-mop-9: delivered (msg_id=m1), idle: went idle after 12s")
        c.expect("mcp wait request", (s.calls[-1][3]["wait"], s.calls[-1][3]["notify"],
                                      s.calls[-1][3]["timeout"]), (30, False, 30 + bus.TIMEOUT))
        s.answers["request"] = {"msg_id": "m1"}
        c.expect("mcp wait not idle", mcp.send(PUPPET, "hi", wait_seconds=30),
                 "pu-mop-9: delivered (msg_id=m1), idle: did not wait it out in 30s")
        # #574: ошибка наблюдения простоя -- не молчаливое «не дождался»:
        # узел называет причину, и она доезжает тем же вердиктом.
        s.answers["request"] = {"msg_id": "m1", "idle": None,
                                "idle_error": "idle observation failed"}
        c.expect("mcp idle error keeps the reason",
                 mcp.send(PUPPET, "hi", wait_seconds=30),
                 "pu-mop-9: delivered (msg_id=m1), idle: idle observation failed")
        mcp.send(PUPPET, "hi", wait_seconds=9999)
        c.expect("mcp wait clamped", s.calls[-1][3]["wait"], 600)
        mcp.send(PUPPET, "hi", wait_seconds=-5)
        c.expect("mcp negative wait", s.calls[-1][3]["wait"], 0)

        c.expect("mcp notify", mcp.send(PUPPET, "hi", notify_when_idle=True),
                 "pu-mop-9: delivered (msg_id=m1); will notify when it frees up")
        c.expect("mcp notify request", s.calls[-1][3]["notify"], True)
        # С ожиданием подписку держать незачем: ответ и так дождётся.
        mcp.send(PUPPET, "hi", notify_when_idle=True, wait_seconds=5)
        c.expect("mcp notify with wait", s.calls[-1][3]["notify"], False)

        # Локальная сессия.
        s.answers["session.send"] = {"msg_id": "m2", "idle": None}
        c.expect("mcp local delivered", mcp.send("s1", "hi"), "s1: delivered (msg_id=m2)")
        c.expect("mcp local call", s.calls[-1],
                 ("session.send", SESS["messagingSocketPath"], "hi",
                  {"priority": "next", "from_name": "host-77", "wait_idle": 0}))
        s.answers["session.send"] = {"msg_id": "m2", "idle": {"state": "idle"}}
        c.expect("mcp local idle", mcp.send("123", "hi", wait_seconds=5),
                 "s1: delivered (msg_id=m2), idle: idle")
        s.answers["session.send"] = {"msg_id": "m2", "idle": None}
        c.expect("mcp local not idle", mcp.send("s1", "hi", wait_seconds=5),
                 "s1: delivered (msg_id=m2), idle: did not wait it out")
        s.alive = False
        c.expect("mcp local dead", mcp.send("s1", "hi"), "s1: inbox not listening — session is dead")
        s.alive = True
        # Несколько сессий с одним именем -- стоп, а не мастер: молча взять
        # первую значит однажды написать не тому.
        c.expect("mcp local ambiguous", mcp.send("twins", "hi"),
                 "send: 'twins' matches several sessions: twins[1], twins[2] — specify pid")

        # Мастер -- всё остальное.
        s.answers["ask"] = {"msg_id": "m3"}
        c.expect("mcp master delivered", mcp.send("host-1", "hi", priority="now"),
                 "host-1: delivered (msg_id=m3)")
        c.expect("mcp master call", s.calls[-1],
                 ("ask", "host-1", "message", {"text": "hi", "priority": "now", "from": "host-77"}))
        s.answers["ask"] = {"error": "master host-1 has no session — nowhere to deliver the note"}
        c.expect("mcp master refusal", mcp.send("host-1", "hi"),
                 "host-1: NOT DELIVERED — master host-1 has no session — nowhere to deliver the note")
        s.answers["ask"] = bus.BusError("master host-1 is not on the bus")
        c.expect("mcp master bus error", mcp.send("host-1", "hi"),
                 "host-1: NOT DELIVERED — master host-1 is not on the bus")

        c.expect("mcp bad priority", mcp.send(PUPPET, "hi", priority="asap"),
                 "priority must be one of now, next, later")
        c.expect("mcp empty", mcp.send(PUPPET, "  "), "empty message, nothing to send")

        # ─── характеристика: mop send ─────────────────────────────────────
        cli_request = {"name": PUPPET, "message": "hi", "priority": "next", "wait": 0,
                       "owner": "anton", "force": False, "timeout": bus.TIMEOUT}
        s.answers["request"] = {"msg_id": "m1"}
        c.expect("cli puppet delivered", cli([PUPPET, "hi"]), (0, "-> pu-mop-9 msg_id=m1\n", ""))
        c.expect("cli puppet request", s.calls[-1], ("request", "n1", "send", cli_request))
        s.answers["request"] = {"msg_id": "m1", "owner_note": NOTE}
        c.expect("cli owner note", cli([PUPPET, "hi"]), (0, f"-> pu-mop-9 msg_id=m1; {NOTE}\n", ""))
        s.answers["request"] = {"error": OWNED}
        c.expect("cli owner refusal", cli([PUPPET, "hi"]),
                 (1, "", f"pu-mop-9: NOT DELIVERED — {OWNED}\n"))
        s.answers["request"] = {"msg_id": "m1"}
        cli([PUPPET, "hi", "--force"])
        c.expect("cli force travels", s.calls[-1][3]["force"], True)
        s.answers["request"] = {"msg_id": "m1", "idle": "went idle after 12s"}
        c.expect("cli wait idle", cli([PUPPET, "hi", "--wait=30"]),
                 (0, "-> pu-mop-9 msg_id=m1\n<- went idle after 12s\n", ""))
        c.expect("cli wait request", (s.calls[-1][3]["wait"], s.calls[-1][3]["timeout"]),
                 (30, 30 + bus.TIMEOUT))
        s.answers["request"] = {"msg_id": "m1"}
        c.expect("cli wait not idle", cli([PUPPET, "hi", "--wait", "30"]),
                 (2, "-> pu-mop-9 msg_id=m1\nwaited 30s — puppet never reported going free\n", ""))
        cli([PUPPET, "hi", "--wait=9999"])
        c.expect("cli wait clamped", s.calls[-1][3]["wait"], 600)
        cli([PUPPET, "hi", "--wait"])
        c.expect("cli bare --wait", s.calls[-1][3]["wait"], 600)
        c.expect("cli quiet", cli([PUPPET, "hi", "--quiet"]), (0, "", ""))
        c.expect("cli quiet not idle", cli([PUPPET, "hi", "--quiet", "--wait=3"]), (2, "", ""))

        s.answers["session.send"] = {"msg_id": "m2", "idle": None}
        c.expect("cli local delivered", cli(["s1", "hi"]), (0, "-> s1 [123] msg_id=m2\n", ""))
        c.expect("cli local call", s.calls[-1],
                 ("session.send", SESS["messagingSocketPath"], "hi",
                  {"priority": "next", "mode": "bypass", "from_name": "mop", "wait_idle": 0}))
        cli(["s1", "hi", "--mode", "prompting", "--wait=9999"])
        c.expect("cli local mode, clamp", (s.calls[-1][3]["mode"], s.calls[-1][3]["wait_idle"]),
                 ("prompting", 600))
        s.answers["session.send"] = {"msg_id": "m2", "idle": {"state": "idle", "detail": "done"}}
        c.expect("cli local idle", cli(["s1", "hi", "--wait=5"]),
                 (0, "-> s1 [123] msg_id=m2\n<- idle: done\n", ""))
        s.answers["session.send"] = {"msg_id": "m2", "idle": None}
        c.expect("cli local not idle", cli(["s1", "hi", "--wait=5"]),
                 (2, "-> s1 [123] msg_id=m2\nwaited 5s — session never reported going idle\n", ""))
        s.alive = False
        c.expect("cli local dead", cli(["s1", "hi"]),
                 (1, "", "s1: inbox not listening — session is dead\n"))
        s.alive = True

    # ─── сам канал ────────────────────────────────────────────────────────
    try:
        from mop.client import channel
    except ImportError as e:
        c.fail("mop/client/channel.py", str(e))
        return c.report("channel")

    c.expect("MAX_WAIT", channel.MAX_WAIT, 600)
    for raw, want in ((None, 0), (0, 0), (30, 30), (9999, 600), (-5, 0)):
        c.expect(f"clamp_wait({raw})", channel.clamp_wait(raw), want)

    # Путь по адресу: папет -- префиксом джоба, дальше своя сессия этого
    # хоста, всё остальное -- инбокс мастера. Ambiguous наверх, не мастеру.
    with Stubs() as s:
        c.expect("route puppet", channel.route("pu-mop-1"), ("puppet", "pu-mop-1"))
        c.expect("route local by name", channel.route("s1"), ("local", SESS))
        c.expect("route local by pid", channel.route("123"), ("local", SESS))
        c.expect("route master", channel.route("host-1"), ("master", "host-1"))
        try:
            got = channel.route("twins")
            c.expect("route ambiguous must raise", got, "Ambiguous")
        except session.Ambiguous:
            pass

        # Своё имя: мастер -- инбокс, папет -- имя клона из каталога сессии.
        with patched_env(CLAUDE_CODE_MESSAGING_SOCKET=SESS["messagingSocketPath"]):
            c.expect("my_session", channel.my_session(), SESS)
            c.expect("my_name master", channel.my_name(True, "host-77"), "host-77")
            s.put(session, "sessions", lambda: [dict(SESS, cwd="/home/u/puppets/pu-mop-4")])
            c.expect("my_name puppet", channel.my_name(False, "host-77"), "pu-mop-4")

    # Вердикт -> текст: доставлено, отказ агента (в том числе владельца),
    # мёртвый инбокс. Отказ -- одна и та же строка у обоих фронтендов.
    for v, want in (
            ({"kind": "puppet", "to": PUPPET, "msg_id": "m1"}, "pu-mop-9: delivered (msg_id=m1)"),
            ({"kind": "puppet", "to": PUPPET, "msg_id": "m1", "owner_note": NOTE},
             f"pu-mop-9: delivered (msg_id=m1); {NOTE}"),
            ({"kind": "puppet", "to": PUPPET, "error": OWNED}, f"pu-mop-9: NOT DELIVERED — {OWNED}"),
            ({"kind": "puppet", "to": PUPPET,
              "error": "pu-mop-9: the body did not answer in 20s", "timeout": True},
             "pu-mop-9: DELIVERY UNKNOWN — pu-mop-9: the body did not answer in 20s"),
            ({"kind": "puppet", "to": PUPPET, "msg_id": "m1", "wait": 30},
             "pu-mop-9: delivered (msg_id=m1), idle: did not wait it out in 30s"),
            ({"kind": "puppet", "to": PUPPET, "msg_id": "m1", "wait": 30,
              "idle": None, "idle_error": "idle observation failed"},
             "pu-mop-9: delivered (msg_id=m1), idle: idle observation failed"),
            ({"kind": "puppet", "to": PUPPET, "msg_id": "m1", "notify": True},
             "pu-mop-9: delivered (msg_id=m1); will notify when it frees up"),
            ({"kind": "local", "to": "s1", "dead": True}, "s1: inbox not listening — session is dead"),
            ({"kind": "local", "to": "s1", "msg_id": "m2", "wait": 5, "idle": {"state": "idle"}},
             "s1: delivered (msg_id=m2), idle: idle"),
            ({"kind": "master", "to": "host-1", "msg_id": "m3"}, "host-1: delivered (msg_id=m3)"),
            ({"kind": "master", "to": "host-1", "error": "gone"}, "host-1: NOT DELIVERED — gone")):
        c.expect(f"text {v}", channel.text(v), want)
    c.expect("failure of delivered", channel.failure({"kind": "puppet", "to": PUPPET, "msg_id": "m1"}), None)
    c.expect("failure of refused", channel.failure({"kind": "puppet", "to": PUPPET, "error": OWNED}),
             f"pu-mop-9: NOT DELIVERED — {OWNED}")
    # Неопределённая доставка (#574 ревью 3 п.5) -- тоже «не доставлено»
    # для канона failure (выход -- с причиной), но своей строкой.
    c.expect("failure of indeterminate",
             channel.failure({"kind": "puppet", "to": PUPPET,
                              "error": "the body did not answer in 20s",
                              "timeout": True}),
             "pu-mop-9: DELIVERY UNKNOWN — the body did not answer in 20s")

    # Копий во фронтендах больше нет: потолок, тексты отказа, своя сессия.
    here = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    for rel in ("mop/cli/core/send.py", "mop/cli/service/mcp.py"):
        with open(os.path.join(here, rel)) as f:
            src = f.read()
        for copy in ("MAX_WAIT = ", "NOT DELIVERED", "inbox not listening",
                     "def master_socket", "def my_session", "def my_name",
                     "def _send_to_", "def _send_locally"):
            c.expect(f"{rel} has no copy of {copy!r}", copy in src, False)
    # STATUS: FIXED — see #148

    # ── #213: адрес мастера несёт логин ──────────────────────────────────
    # HYPOTHESIS: адрес <хост>-<pid> логина не несёт, и подписку на свой
    # инбокс не сузить до своего логина -- любой мастер проекта слушает все.
    # SOLUTION: адрес -- <токен логина>.<хост>-<pid>; папет отвечает на адрес
    # из конверта (from-name), агент -- в reply_to, и оба уже нового вида.
    from mop.common import busnames
    from mop.server import operators
    local = f"{os.uname().nodename}-{os.getpid()}"
    c.expect("master_id of a person", mcp.master_id("anton.ermak"), f"anton%2Eermak.{local}")
    c.expect("master_id of the server's service", mcp.master_id("service"), f"service.{local}")
    c.expect("master_id without creds", mcp.master_id(None), local)
    mine = bus.inbox(mcp.master_id("anton.ermak"), "mop")
    perms = operators.permissions({"role": "user", "projects": ["mop"]}, "anton.ermak")
    c.expect("the master's inbox is its own to subscribe",
             [m for m in perms["allow"] if ".master." in m and m.endswith(".>")],
             ["mop.mop.master.anton%2Eermak.>"])
    c.expect("the inbox lies under it", mine.startswith("mop.mop.master.anton%2Eermak."), True)
    with Stubs() as s:
        addr = "anton%2Eermak.wate.lan-7"
        c.expect("route a new-form address", channel.route(addr), ("master", addr))
        s.put(mcp, "MASTER_ID", "bob.host-77")
        s.put(mcp, "MY_INBOX", "mop.mop.master.bob.host-77.inbox")
        s.answers["ask"] = {"msg_id": "m4"}
        mcp.send(addr, "report")
        c.expect("the reply goes to the envelope's address", s.calls[-1][:2], ("ask", addr))
        c.expect("and names the sender by its full address", s.calls[-1][3]["from"], "bob.host-77")
        s.answers["request"] = {"msg_id": "m5"}
        mcp.send(PUPPET, "go", notify_when_idle=True)
        c.expect("agents get the new-form inbox as reply_to", s.calls[-1][3]["reply_to"],
                 "mop.mop.master.bob.host-77.inbox")

    # Отказ шины в подписке на свой инбокс -- громко: строка в сессию и в
    # stderr MCP-сервера. Прочие ошибки и чужие субъекты -- мимо.
    mine, who = "mop.mop.master.bob.h-1.inbox", "mop.mop.master.all.inbox"
    for text, want in (
            (f'nats: permissions violation for subscription to "{mine}" (sid "1")', mine),
            (f'nats: permissions violation for subscription to "{who}"', who),
            ('nats: permissions violation for subscription to "mop.mop.events"', None),
            (f'nats: permissions violation for publish to "{mine}"', None),
            ("nats: unexpected EOF", None)):
        c.expect(f"refused_inbox {text!r}", mcp.refused_inbox(text, (mine, who)), want)
    with Stubs() as s:
        s.put(mcp, "MASTER_ID", "bob.h-1")
        s.put(mcp, "MY_INBOX", mine)
        err = io.StringIO()
        with patched_env(CLAUDE_CODE_MESSAGING_SOCKET=SESS["messagingSocketPath"]), \
                contextlib.redirect_stderr(err):
            mcp.on_bus_error(f'nats: permissions violation for subscription to "{mine}"')
            mcp.on_bus_error(f'nats: permissions violation for subscription to "{mine}"')
            mcp.on_bus_error("nats: unexpected EOF")
        pushed = [call for call in s.calls if call[0] == "session.send"]
        c.expect("the refusal is pushed into the session once", len(pushed), 1)
        line = pushed[0][2] if pushed else ""
        c.expect("it names the inbox and the cure",
                 mine in line and "restart the mop MCP server" in line, True)
        c.expect("the same line goes to stderr", err.getvalue().strip(), line.strip())
    # STATUS: FIXED — see #213

    # HYPOTHESIS: сессия мастера может умереть после поиска сокета; его
    # локальный путь выезжает в отказ отправителю через шину.
    # SOLUTION: отсутствие сокета и отказ соединения дают один безопасный
    # вердикт с полезным действием; прочие ошибки не глушатся.
    # RESULT: путь не уходит адресату при обеих формах смерти сессии.
    # STATUS: FIXED — see #437
    missing = "/run/user/1000/private/master-123.sock"
    message = {"verb": "message", "text": "report", "from": "pu-mop-9"}
    with patched(channel, master_socket=lambda: missing), \
            patched(session, send=lambda *a, **k: (_ for _ in ()).throw(
                ConnectionError(f"inbox not found: {missing} — session died or moved"))):
        try:
            outcome = mcp.on_inbox(message)
        except ConnectionError as e:
            c.fail("#437 dead inbox must return a verdict instead of raising", str(e))
            outcome = {}
    want = {"error": "master session unavailable — choose a live master"}
    c.expect("#437 dead inbox names action without path", outcome, want)
    with patched(channel, master_socket=lambda: None):
        c.expect("#437 missing inbox uses the same refusal", mcp.on_inbox(message), want)

    # ─── #226: --check не выдаёт свой адрес за адрес живого сервера ─────────
    # HYPOTHESIS: `mop mcp --check` -- отдельный процесс; MASTER_ID строится
    # из его собственного pid, и напечатанный адрес с инбоксом не слушает
    # никто. Отданный папету, он уводит ответ в пустоту.
    # SOLUTION: --check печатает профиль, число инструментов, шину и сокет
    # сессии -- и ничего похожего на адрес. Адрес мастера берётся только из
    # таблицы мастеров в agents или из конверта письма.
    import re
    with patched(mcp, MASTER=True):
        text, _, code = run_command(mcp.main, ["--check"])
    c.expect("--check succeeds", code, 0)
    c.expect("--check still names the profile and the session",
             "profile " in text and "session: " in text, True)
    c.expect("--check prints no <login>.<host>-<pid> address",
             re.findall(r"\b[\w-]+\.[\w-]+-\d+\b", text), [])
    c.expect("--check prints no .inbox subject", ".inbox" in text, False)
    c.expect("--check does not print its own MASTER_ID", mcp.MASTER_ID in text, False)
    described = {t.name: t.description for t in mcp.app._tool_manager.list_tools()}
    c.expect("agents says where a master's address comes from",
             "only from the masters table" in (described.get("agents") or ""), True)
    # RESULT: красный на старом --check (адрес master.<хост>-<pid> и .inbox в
    # выводе), зелёный после.
    # STATUS: FIXED — see #226

    # ─── #481: --check предупреждает про admin-срез без MOP_PROJECT ─────
    # HYPOTHESIS: claude с MCP mop, запущенный не через `mop master`, живёт
    # без MOP_PROJECT; его who-ответ уходит в admin-срез, опрос проекта его
    # не находит, и сессия невидима в «Мастерах» дашборда (03.10: mop-b8
    # полдня не была видна). --check молчал про это же.
    # SOLUTION: --check мастера без проекта печатает предупреждение: срез
    # admin, в списке мастеров проекта сессии не будет; с MOP_PROJECT --
    # прежняя строка без предупреждения.
    with patched(mcp, MASTER=True), \
            patched(bus, config=lambda: {"url": "nats://x"}, PROJECT=bus.ADMIN):
        text, _, code = run_command(mcp.main, ["--check"])
    c.expect("#481 check still succeeds without project", code, 0)
    c.expect("#481 the admin slice is named",
             any(w in text.lower() for w in ("admin", "оператора")), True)
    c.expect("#481 the invisibility is named",
             "masters" in text.lower() or "мастер" in text.lower(), True)
    with patched(mcp, MASTER=True), \
            patched(bus, config=lambda: {"url": "nats://x"}, PROJECT="mop"):
        text, _, _ = run_command(mcp.main, ["--check"])
    c.expect("#481 a project profile carries no warning",
             "warning" in text.lower(), False)
    # RESULT: красный на молчащем --check, зелёный после.
    # STATUS: FIXED — see #481

    # ─── интеграционная ветка мастера в инструкциях MCP (#250) ────────────
    # HYPOTHESIS: #249 отдал ветку человека сессии мастера переменной
    # MOP_BRANCH и одной фразой в шаге 0 скилла; мастер rudesktop, стартовав
    # с MOP_BRANCH=swarm, всё равно вывел ветку по origin/HEAD и истории
    # merge и назвал beta3.1. Инструкции сервера `mop mcp` сессия читает при
    # каждом старте без всякого скилла, и о ветке они молчали.
    # SOLUTION: mcp.instructions(environ) -- чистая функция текста; с
    # MOP_BRANCH в нём абзац, называющий ветку базой веток тикетов, целью
    # landing и тем, что мастер называет в диспатче, и что ветка по умолчанию
    # репозитория тут не голосует. Без переменной -- прежний текст.
    # STATUS: FIXED — see #250
    plain = mcp.instructions({"MOP_PROJECT": "rudesktop"})
    mine = mcp.instructions({"MOP_PROJECT": "rudesktop", "MOP_BRANCH": "swarm"})
    c.expect("#250 no branch, no branch talk", "integration branch" in plain.lower(), False)
    c.expect("#250 the branch is named", "`swarm`" in mine, True)
    c.expect("#250 named as the integration branch",
             "integration branch" in mine.lower(), True)
    c.expect("#250 the default branch does not decide",
             "default branch" in mine.lower(), True)
    c.expect("#250 base of ticket branches and landing target",
             "origin/swarm" in mine and "land" in mine.lower(), True)
    c.expect("#250 the plain text is kept verbatim in front", mine.startswith(plain), True)

    # HYPOTHESIS: MCP повторяет внутреннее устройство канала в инструкциях
    # и каждом описании; сокращение докстрингов командлетов его не затрагивает.
    # SOLUTION: инструкции и четыре рукописных описания говорят о выборе
    # инструмента и последствиях force, не о транспортном механизме.
    # RESULT: модель получает короткие действия без потери ограничения.
    # STATUS: FIXED — see #434
    shown = {t.name: t.description for t in mcp.app._tool_manager.list_tools()}
    for name in ("agents", "send", "tail"):
        c.check(f"#434 {name} still described", bool(shown.get(name)))
        c.check(f"#434 {name} hides transport internals", not any(
            word in (shown.get(name) or "") for word in
            ("node agent", "skipSlashCommands", "bus", "tmux", "neighboring node")))
    c.check("#434 send retains force/ownership warning", all(
        word in shown["send"] for word in ("force", "owner")))
    c.check("#434 agents retains free-work rule", all(
        word in shown["agents"] for word in ("free", "unsaved")))
    # В профиле папета slash не зарегистрирован: читаем объявление инструмента.
    import inspect
    slash_source = inspect.getsource(mcp.slash)
    c.check("#434 slash omits transport implementation", "skipSlashCommands" not in slash_source)
    c.check("#434 slash retains allowed-command list and force", all(
        word in slash_source for word in ("SLASH_ALLOWED", "force=true")))
    c.check("#434 instructions avoid implementation history", not any(
        word in plain for word in ("Nomad", "NATS", "bus", "neighboring node")))
    c.check("#434 --check describes local settings, not a live reachability test",
            "local profile" in (mcp.__doc__ or "") and
            "can reach" not in (mcp.__doc__ or ""))
    c.check("#434 instructions retain dispatch check", all(
        word in plain for word in ("agents", "send", "unsaved")))

    c.check("#264 one envelope, one fallback, request_many(verb, nodes)",
            check_bus_envelope_264(c))
    c.check("#369 a stale permissions error does not explain a later timeout",
            check_stale_permissions_error_369(c))
    c.check("#371 agents(project) matches the project, not a substring of origin",
            check_agents_project_371(c))

    try:
        check_role_channel_585(c)
    except Exception as e:  # красный -- проваленной проверкой, а не трассой
        c.fail("#585 role channel", f"{type(e).__name__}: {e}")
    try:
        check_role_api_585(c)
    except Exception as e:
        c.fail("#585 role api", f"{type(e).__name__}: {e}")
    # RESULT: красный на сегодняшнем маршруте (grant -- мастер-профиль,
    # owner/force грантом, from-name инбоксом, легаси по rpc), зелёный после.
    # Решение мастера по C5 сменило одно ожидание до реализации: инбокс
    # процесса роли остаётся транспортом reply_to, адрес -- имя джоба.
    # STATUS: FIXED — see #585

    return c.report("channel")


if __name__ == "__main__":
    sys.exit(main())
