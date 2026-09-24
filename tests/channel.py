#!/usr/bin/env python3
"""Канал сообщений без пула: python3 tests/channel.py

Две части. Первая -- характеристика: оба фронтенда (`mop send` и инструмент
`send` в MCP) прогоняются на заглушках шины и сокета, и их тексты и запросы
агенту приколоты байт в байт такими, какими они были до #148. Вторая --
сам канал (mop/channel.py): выбор пути по адресу, потолок ожидания, вердикт
-> текст.

HYPOTHESIS (#148): маршрутизация трёх путей и поиск своей сессии жили во
фронтенде MCP, а MAX_WAIT, обрезка ожидания и тексты «NOT DELIVERED — » и
«inbox not listening — session is dead» -- ещё и копией в cli/core/send.py.
SOLUTION: mop/channel.py -- send_to_puppet, send_local, send_to_master,
MAX_WAIT, своя сессия; возвращает вердикт (данные), не печатает. Фронтенды
только рисуют.
"""
import contextlib
import io
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
os.environ.setdefault("MOP_SERVER_LAN", "127.0.0.1")

from mop import bus, puppets, session  # noqa: E402
from mop.cli import lib  # noqa: E402
from mop.cli.core import send as cli_send  # noqa: E402
from mop.cli.service import mcp  # noqa: E402

PUPPET = "pu-mop-9"
NOTE = "was led by olga; now yours"
OWNED = "pu-mop-9 is led by olga (work in its clone) — force=true takes it over"
SESS = {"name": "s1", "pid": 123, "messagingSocketPath": "/run/user/1000/cc-socks/123.sock",
        "cwd": "/w"}


class Stubs:
    """Заглушки шины, сокета и ростера. Что спросили -- в calls, что ответить
    -- в answers по пути."""

    def __init__(self):
        self.calls = []
        self.answers = {}
        self.alive = True
        self.saved = []

    def put(self, mod, name, fn):
        self.saved.append((mod, name, getattr(mod, name)))
        setattr(mod, name, fn)

    def install(self):
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

    def restore(self):
        for mod, name, value in reversed(self.saved):
            setattr(mod, name, value)


def cli(argv):
    """`mop send` -> (код, stdout, stderr). sys.exit(строка) -- как у
    интерпретатора: строка в stderr, код 1."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli_send.main(argv) or 0
        except SystemExit as e:
            if isinstance(e.code, str):
                print(e.code, file=sys.stderr)
                code = 1
            else:
                code = e.code or 0
    return code, out.getvalue(), err.getvalue()


def main():
    cases = bad = 0

    def check(what, got, want):
        nonlocal cases, bad
        cases += 1
        if got != want:
            bad += 1
            print(f"FAILED  {what}:\n   got {got!r}\n  want {want!r}")

    # ─── характеристика: MCP ──────────────────────────────────────────────
    s = Stubs().install()
    try:
        mcp_request = {"name": PUPPET, "message": "hi", "priority": "next", "wait": 0,
                       "notify": False, "from_name": "host-77", "owner": "anton",
                       "force": False, "reply_to": "mop.mop.master.host-77.inbox",
                       "timeout": bus.TIMEOUT}
        s.answers["request"] = {"msg_id": "m1"}
        check("mcp puppet delivered", mcp.send(PUPPET, "hi"), "pu-mop-9: delivered (msg_id=m1)")
        check("mcp puppet request", s.calls[-1], ("request", "n1", "send", mcp_request))

        s.answers["request"] = {"msg_id": "m1", "owner_note": NOTE}
        check("mcp owner note", mcp.send(PUPPET, "hi"),
              f"pu-mop-9: delivered (msg_id=m1); {NOTE}")

        s.answers["request"] = {"error": OWNED}
        check("mcp owner refusal", mcp.send(PUPPET, "hi"), f"pu-mop-9: NOT DELIVERED — {OWNED}")
        s.answers["request"] = {"msg_id": "m1"}
        mcp.send(PUPPET, "hi", force=True)
        check("mcp force travels", s.calls[-1][3]["force"], True)

        s.answers["request"] = bus.BusError("node agent n1 did not answer in 20s")
        check("mcp bus error", mcp.send(PUPPET, "hi"),
              "pu-mop-9: NOT DELIVERED — node agent n1 did not answer in 20s")

        s.answers["request"] = {"msg_id": "m1", "idle": "went idle after 12s"}
        check("mcp wait idle", mcp.send(PUPPET, "hi", wait_seconds=30),
              "pu-mop-9: delivered (msg_id=m1), idle: went idle after 12s")
        check("mcp wait request", (s.calls[-1][3]["wait"], s.calls[-1][3]["notify"],
                                   s.calls[-1][3]["timeout"]), (30, False, 30 + bus.TIMEOUT))
        s.answers["request"] = {"msg_id": "m1"}
        check("mcp wait not idle", mcp.send(PUPPET, "hi", wait_seconds=30),
              "pu-mop-9: delivered (msg_id=m1), idle: did not wait it out in 30s")
        mcp.send(PUPPET, "hi", wait_seconds=9999)
        check("mcp wait clamped", s.calls[-1][3]["wait"], 600)
        mcp.send(PUPPET, "hi", wait_seconds=-5)
        check("mcp negative wait", s.calls[-1][3]["wait"], 0)

        check("mcp notify", mcp.send(PUPPET, "hi", notify_when_idle=True),
              "pu-mop-9: delivered (msg_id=m1); will notify when it frees up")
        check("mcp notify request", s.calls[-1][3]["notify"], True)
        # С ожиданием подписку держать незачем: ответ и так дождётся.
        mcp.send(PUPPET, "hi", notify_when_idle=True, wait_seconds=5)
        check("mcp notify with wait", s.calls[-1][3]["notify"], False)

        # Локальная сессия.
        s.answers["session.send"] = {"msg_id": "m2", "idle": None}
        check("mcp local delivered", mcp.send("s1", "hi"), "s1: delivered (msg_id=m2)")
        check("mcp local call", s.calls[-1],
              ("session.send", SESS["messagingSocketPath"], "hi",
               {"priority": "next", "from_name": "host-77", "wait_idle": 0}))
        s.answers["session.send"] = {"msg_id": "m2", "idle": {"state": "idle"}}
        check("mcp local idle", mcp.send("123", "hi", wait_seconds=5),
              "s1: delivered (msg_id=m2), idle: idle")
        s.answers["session.send"] = {"msg_id": "m2", "idle": None}
        check("mcp local not idle", mcp.send("s1", "hi", wait_seconds=5),
              "s1: delivered (msg_id=m2), idle: did not wait it out")
        s.alive = False
        check("mcp local dead", mcp.send("s1", "hi"), "s1: inbox not listening — session is dead")
        s.alive = True
        # Несколько сессий с одним именем -- стоп, а не мастер: молча взять
        # первую значит однажды написать не тому.
        check("mcp local ambiguous", mcp.send("twins", "hi"),
              "send: 'twins' matches several sessions: twins[1], twins[2] — specify pid")

        # Мастер -- всё остальное.
        s.answers["ask"] = {"msg_id": "m3"}
        check("mcp master delivered", mcp.send("host-1", "hi", priority="now"),
              "host-1: delivered (msg_id=m3)")
        check("mcp master call", s.calls[-1],
              ("ask", "host-1", "message", {"text": "hi", "priority": "now", "from": "host-77"}))
        s.answers["ask"] = {"error": "master host-1 has no session — nowhere to deliver the note"}
        check("mcp master refusal", mcp.send("host-1", "hi"),
              "host-1: NOT DELIVERED — master host-1 has no session — nowhere to deliver the note")
        s.answers["ask"] = bus.BusError("master host-1 is not on the bus")
        check("mcp master bus error", mcp.send("host-1", "hi"),
              "host-1: NOT DELIVERED — master host-1 is not on the bus")

        check("mcp bad priority", mcp.send(PUPPET, "hi", priority="asap"),
              "priority must be one of now, next, later")
        check("mcp empty", mcp.send(PUPPET, "  "), "empty message, nothing to send")

        # ─── характеристика: mop send ─────────────────────────────────────
        cli_request = {"name": PUPPET, "message": "hi", "priority": "next", "wait": 0,
                       "owner": "anton", "force": False, "timeout": bus.TIMEOUT}
        s.answers["request"] = {"msg_id": "m1"}
        check("cli puppet delivered", cli([PUPPET, "hi"]), (0, "-> pu-mop-9 msg_id=m1\n", ""))
        check("cli puppet request", s.calls[-1], ("request", "n1", "send", cli_request))
        s.answers["request"] = {"msg_id": "m1", "owner_note": NOTE}
        check("cli owner note", cli([PUPPET, "hi"]), (0, f"-> pu-mop-9 msg_id=m1; {NOTE}\n", ""))
        s.answers["request"] = {"error": OWNED}
        check("cli owner refusal", cli([PUPPET, "hi"]),
              (1, "", f"pu-mop-9: NOT DELIVERED — {OWNED}\n"))
        s.answers["request"] = {"msg_id": "m1"}
        cli([PUPPET, "hi", "--force"])
        check("cli force travels", s.calls[-1][3]["force"], True)
        s.answers["request"] = {"msg_id": "m1", "idle": "went idle after 12s"}
        check("cli wait idle", cli([PUPPET, "hi", "--wait=30"]),
              (0, "-> pu-mop-9 msg_id=m1\n<- went idle after 12s\n", ""))
        check("cli wait request", (s.calls[-1][3]["wait"], s.calls[-1][3]["timeout"]),
              (30, 30 + bus.TIMEOUT))
        s.answers["request"] = {"msg_id": "m1"}
        check("cli wait not idle", cli([PUPPET, "hi", "--wait", "30"]),
              (2, "-> pu-mop-9 msg_id=m1\nwaited 30s — puppet never reported going free\n", ""))
        cli([PUPPET, "hi", "--wait=9999"])
        check("cli wait clamped", s.calls[-1][3]["wait"], 600)
        cli([PUPPET, "hi", "--wait"])
        check("cli bare --wait", s.calls[-1][3]["wait"], 600)
        check("cli quiet", cli([PUPPET, "hi", "--quiet"]), (0, "", ""))
        check("cli quiet not idle", cli([PUPPET, "hi", "--quiet", "--wait=3"]), (2, "", ""))

        s.answers["session.send"] = {"msg_id": "m2", "idle": None}
        check("cli local delivered", cli(["s1", "hi"]), (0, "-> s1 [123] msg_id=m2\n", ""))
        check("cli local call", s.calls[-1],
              ("session.send", SESS["messagingSocketPath"], "hi",
               {"priority": "next", "mode": "bypass", "from_name": "mop", "wait_idle": 0}))
        cli(["s1", "hi", "--mode", "prompting", "--wait=9999"])
        check("cli local mode, clamp", (s.calls[-1][3]["mode"], s.calls[-1][3]["wait_idle"]),
              ("prompting", 600))
        s.answers["session.send"] = {"msg_id": "m2", "idle": {"state": "idle", "detail": "done"}}
        check("cli local idle", cli(["s1", "hi", "--wait=5"]),
              (0, "-> s1 [123] msg_id=m2\n<- idle: done\n", ""))
        s.answers["session.send"] = {"msg_id": "m2", "idle": None}
        check("cli local not idle", cli(["s1", "hi", "--wait=5"]),
              (2, "-> s1 [123] msg_id=m2\nwaited 5s — session never reported going idle\n", ""))
        s.alive = False
        check("cli local dead", cli(["s1", "hi"]),
              (1, "", "s1: inbox not listening — session is dead\n"))
        s.alive = True
    finally:
        s.restore()

    # ─── сам канал ────────────────────────────────────────────────────────
    try:
        from mop import channel
    except ImportError as e:
        print(f"FAILED  mop/channel.py: {e}")
        return 1

    check("MAX_WAIT", channel.MAX_WAIT, 600)
    for raw, want in ((None, 0), (0, 0), (30, 30), (9999, 600), (-5, 0)):
        check(f"clamp_wait({raw})", channel.clamp_wait(raw), want)

    # Путь по адресу: папет -- префиксом джоба, дальше своя сессия этого
    # хоста, всё остальное -- инбокс мастера. Ambiguous наверх, не мастеру.
    s = Stubs().install()
    try:
        check("route puppet", channel.route("pu-mop-1"), ("puppet", "pu-mop-1"))
        check("route local by name", channel.route("s1"), ("local", SESS))
        check("route local by pid", channel.route("123"), ("local", SESS))
        check("route master", channel.route("host-1"), ("master", "host-1"))
        try:
            got = channel.route("twins")
            check("route ambiguous must raise", got, "Ambiguous")
        except session.Ambiguous:
            pass

        # Своё имя: мастер -- инбокс, папет -- имя клона из каталога сессии.
        keep = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
        os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = SESS["messagingSocketPath"]
        try:
            check("my_session", channel.my_session(), SESS)
            check("my_name master", channel.my_name(True, "host-77"), "host-77")
            s.put(session, "sessions", lambda: [dict(SESS, cwd="/home/u/puppets/pu-mop-4")])
            check("my_name puppet", channel.my_name(False, "host-77"), "pu-mop-4")
        finally:
            if keep is None:
                os.environ.pop("CLAUDE_CODE_MESSAGING_SOCKET", None)
            else:
                os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = keep
    finally:
        s.restore()

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
        check(f"text {v}", channel.text(v), want)
    check("failure of delivered", channel.failure({"kind": "puppet", "to": PUPPET, "msg_id": "m1"}), None)
    check("failure of refused", channel.failure({"kind": "puppet", "to": PUPPET, "error": OWNED}),
          f"pu-mop-9: NOT DELIVERED — {OWNED}")

    # Копий во фронтендах больше нет: потолок, тексты отказа, своя сессия.
    here = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    for rel in ("mop/cli/core/send.py", "mop/cli/service/mcp.py"):
        with open(os.path.join(here, rel)) as f:
            src = f.read()
        for copy in ("MAX_WAIT = ", "NOT DELIVERED", "inbox not listening",
                     "def master_socket", "def my_session", "def my_name",
                     "def _send_to_", "def _send_locally"):
            check(f"{rel} has no copy of {copy!r}", copy in src, False)
    # STATUS: FIXED — see #148

    # ── #213: адрес мастера несёт логин ──────────────────────────────────
    # HYPOTHESIS: адрес <хост>-<pid> логина не несёт, и подписку на свой
    # инбокс не сузить до своего логина -- любой мастер проекта слушает все.
    # SOLUTION: адрес -- <токен логина>.<хост>-<pid>; папет отвечает на адрес
    # из конверта (from-name), агент -- в reply_to, и оба уже нового вида.
    from mop import busnames, operators
    local = f"{os.uname().nodename}-{os.getpid()}"
    check("master_id of a person", mcp.master_id("anton.ermak"), f"anton%2Eermak.{local}")
    check("master_id of the server's service", mcp.master_id("service"), f"service.{local}")
    check("master_id without creds", mcp.master_id(None), local)
    mine = bus.inbox(mcp.master_id("anton.ermak"), "mop")
    perms = operators.permissions({"role": "user", "projects": ["mop"]}, "anton.ermak")
    check("the master's inbox is its own to subscribe",
          [m for m in perms["allow"] if ".master." in m and m.endswith(".>")],
          ["mop.mop.master.anton%2Eermak.>"])
    check("the inbox lies under it", mine.startswith("mop.mop.master.anton%2Eermak."), True)
    s = Stubs().install()
    try:
        addr = "anton%2Eermak.wate.lan-7"
        check("route a new-form address", channel.route(addr), ("master", addr))
        s.put(mcp, "MASTER_ID", "bob.host-77")
        s.put(mcp, "MY_INBOX", "mop.mop.master.bob.host-77.inbox")
        s.answers["ask"] = {"msg_id": "m4"}
        mcp.send(addr, "report")
        check("the reply goes to the envelope's address", s.calls[-1][:2], ("ask", addr))
        check("and names the sender by its full address", s.calls[-1][3]["from"], "bob.host-77")
        s.answers["request"] = {"msg_id": "m5"}
        mcp.send(PUPPET, "go", notify_when_idle=True)
        check("agents get the new-form inbox as reply_to", s.calls[-1][3]["reply_to"],
              "mop.mop.master.bob.host-77.inbox")
    finally:
        s.restore()

    # Отказ шины в подписке на свой инбокс -- громко: строка в сессию и в
    # stderr MCP-сервера. Прочие ошибки и чужие субъекты -- мимо.
    mine, who = "mop.mop.master.bob.h-1.inbox", "mop.mop.master.all.inbox"
    for text, want in (
            (f'nats: permissions violation for subscription to "{mine}" (sid "1")', mine),
            (f'nats: permissions violation for subscription to "{who}"', who),
            ('nats: permissions violation for subscription to "mop.mop.events"', None),
            (f'nats: permissions violation for publish to "{mine}"', None),
            ("nats: unexpected EOF", None)):
        check(f"refused_inbox {text!r}", mcp.refused_inbox(text, (mine, who)), want)
    s = Stubs().install()
    try:
        s.put(mcp, "MASTER_ID", "bob.h-1")
        s.put(mcp, "MY_INBOX", mine)
        keep = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
        os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = SESS["messagingSocketPath"]
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                mcp.on_bus_error(f'nats: permissions violation for subscription to "{mine}"')
                mcp.on_bus_error(f'nats: permissions violation for subscription to "{mine}"')
                mcp.on_bus_error("nats: unexpected EOF")
        finally:
            if keep is None:
                os.environ.pop("CLAUDE_CODE_MESSAGING_SOCKET", None)
            else:
                os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = keep
        pushed = [c for c in s.calls if c[0] == "session.send"]
        check("the refusal is pushed into the session once", len(pushed), 1)
        line = pushed[0][2] if pushed else ""
        check("it names the inbox and the cure",
              mine in line and "restart the mop MCP server" in line, True)
        check("the same line goes to stderr", err.getvalue().strip(), line.strip())
    finally:
        s.restore()
    # STATUS: FIXED — see #213

    # ─── #226: --check не выдаёт свой адрес за адрес живого сервера ─────────
    # HYPOTHESIS: `mop mcp --check` -- отдельный процесс; MASTER_ID строится
    # из его собственного pid, и напечатанный адрес с инбоксом не слушает
    # никто. Отданный папету, он уводит ответ в пустоту.
    # SOLUTION: --check печатает профиль, число инструментов, шину и сокет
    # сессии -- и ничего похожего на адрес. Адрес мастера берётся только из
    # таблицы мастеров в agents или из конверта письма.
    import re
    keep = mcp.MASTER
    mcp.MASTER = True
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mcp.main(["--check"])
    finally:
        mcp.MASTER = keep
    text = out.getvalue()
    check("--check succeeds", code, 0)
    check("--check still names the profile and the session",
          "profile " in text and "session: " in text, True)
    check("--check prints no <login>.<host>-<pid> address",
          re.findall(r"\b[\w-]+\.[\w-]+-\d+\b", text), [])
    check("--check prints no .inbox subject", ".inbox" in text, False)
    check("--check does not print its own MASTER_ID", mcp.MASTER_ID in text, False)
    described = {t.name: t.description for t in mcp.app._tool_manager.list_tools()}
    check("agents says where a master's address comes from",
          "only from the masters table" in (described.get("agents") or ""), True)
    # RESULT: красный на старом --check (адрес master.<хост>-<pid> и .inbox в
    # выводе), зелёный после.
    # STATUS: FIXED — see #226

    print(f"channel: {cases - bad}/{cases}" + (" FAILED" if bad else " ok"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
