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
    собирать {"verb": ...} руками; keys.results_from и bus.failure разбирали
    ответ узла одной и той же тройной проверкой каждый сам.
    SOLUTION: один конверт (bus.envelope), одна корутина запроса с переходом
    (bus.arequest) на всех путях; request_many(verb, узлы, **поля) и
    request_stream(verb, {ключ: (узел, поля)}, **поля) -- той же формы, что
    request; разбор ответа -- bus.verdict. STATUS: FIXED — see #264

    Соединение -- заглушка: субъект с логином отвечает NoRespondersError,
    прежний -- эхом того, что пришло."""
    import json
    from mop.client import keys
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
            ({"kind": "puppet", "to": PUPPET, "msg_id": "m1", "wait": 30},
             "pu-mop-9: delivered (msg_id=m1), idle: did not wait it out in 30s"),
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

    c.check("#264 one envelope, one fallback, request_many(verb, nodes)",
            check_bus_envelope_264(c))

    return c.report("channel")


if __name__ == "__main__":
    sys.exit(main())
