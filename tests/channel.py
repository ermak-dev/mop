#!/usr/bin/env python3
"""Канал сообщений без пула: python3 tests/channel.py

#793 (эпик #623): характеристика канала перенесена с удалённого легаси-фронтенда
(`mop.cli.service.mcp.send` и `mop send` поверх шины/сокета) на канонический
потребительский шов. Свойства #148/#213/#585 сохранены, но исполняются там, где
они живут сегодня:

  * `mop send <id> <text|->` -- конечная HTTPS-команда POST /api/v1/messages
    (`tests/control_consumers.py` держит форму конверта; здесь -- маршрут,
    отказы, границы и вердикт операции);
  * приватная доставка отчёта -- POST /api/v1/reports и собственный WSS-потребитель
    (`tests/private_inbox_client.py` на ветке #773); на дереве эпика private inbox
    явно не связан, и это зафиксировано точным отказом;
  * `mop.cli.service.mcp` больше не несёт инструментов: каталог строится из
    деклараций командлетов (`tests/modern_mcp.py`, `tests/control_consumers.py`);
  * `mop/client/channel.py` остаётся: route по адресу, потолок ожидания,
    вердикт -> текст, `send_to_puppet` как тонкая обёртка `ops.send_to_puppet`.

`unknown` -- честный терминальный исход обычной native-доставки на 2.1.302
(решение по #785, отчёт /tmp/mop-623-task-specs/785-native-wire-contract-report.md):
`accepted` не выдаётся за доставку, `delivered` не выводится ни из тишины, ни из
записи, ни из простоя. Ни один старый сценарий не удалён молча: superseded-строки
называют проверку-замену, unsupported-строки -- точный текст отказа.

HYPOTHESIS (#148): маршрутизация трёх путей и поиск своей сессии жили во
фронтенде MCP, а MAX_WAIT, обрезка ожидания и тексты «NOT DELIVERED — » и
«inbox not listening — session is dead» -- ещё и копией в cli/core/send.py.
SOLUTION: mop/client/channel.py -- вердикт (данные), не печать.
STATUS: FIXED — see #148
"""
import contextlib
import os
import re
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, Msg, patched, patched_env, restored, run_command  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
os.environ.setdefault("MOP_SERVER_LAN", "127.0.0.1")

import json  # noqa: E402

from _control_fixture import (Fixture, Response, KEY, EPOCH, TOKEN, PUPPET,  # noqa: E402
                              INCARNATION, DEADLINE, PROJECT, OP, NODE,
                              send_operation, puppet_row)
from mop import cli, session  # noqa: E402
from mop.cli import _control, _mcp, lib  # noqa: E402
from mop.client import channel, control as api  # noqa: E402
from mop.common import bus, busnames, ops, puppets  # noqa: E402

# Канонический потребительский контекст: transient HTTPS, без .env и шины.
CONTEXT = api.ServerContext("https://controller.example", TOKEN)
SESS = {"name": "s1", "pid": 123, "messagingSocketPath": "/run/user/1000/cc-socks/123.sock",
        "cwd": "/w"}

# Точные тексты канонических отказов: их читает человек и модель, поэтому
# проверка держит их байт в байт, а не «содержит unsupported».
UNSUPPORTED_LOCAL = ("unsupported: local recipients/listing, legacy routing/permission mode, takeover and "
                     "idle notifications (trusted holder endpoint is not bound)")
REFUSAL_ROLE_WS = ("unsupported: role creation/readiness and private WS terminal is not bound in this release")
REFUSAL_TAIL = "unsupported: finite authorized tail backend API is not bound in this release"
REFUSAL_TERMINAL = ("unsupported: private terminal physical resolver/capability discovery is not bound "
                    "in this release")
REFUSAL_LOCAL_TARGET = "send requires canonical resource ids, not local recipients or puppet names"
REFUSAL_NO_CONTEXT = ("no HTTPS server context: set MOP_API_ORIGIN and MOP_API_TOKEN "
                      "(the origin may also come from --api-origin)")
BINDING = ["--epoch", EPOCH, "--incarnation", INCARNATION, "--expected-revision", "7"]
SEND_ARGS = ["send", PUPPET, "hi", *BINDING, "--request-id", KEY, "--not-after", DEADLINE]
PRIVATE_INBOX = {"available": False, "reason": "not_bound", "issue": 649}


class Stubs:
    """Заглушки сессий этого хоста: что спросили -- в calls."""

    def __init__(self):
        self.calls = []
        self.alive = True
        self.stack = contextlib.ExitStack()

    def __enter__(self):
        def find(target):
            if target == SESS["name"] or target == str(SESS["pid"]):
                return SESS
            if target == "twins":
                raise session.Ambiguous("'twins' matches several sessions: twins[1], twins[2] — specify pid")
            raise LookupError(f"session not found: {target}")

        self.stack.enter_context(patched(session, find=find))
        self.stack.enter_context(patched(session, socket_alive=lambda path: self.alive))
        self.stack.enter_context(patched(session, sessions=lambda: [SESS]))
        return self

    def __exit__(self, *exc):
        self.stack.close()


def canonical(argv, responses=(), entry=None, connect_failure=False):
    """Канонический командлет в процессе на заглушке HTTPS -> (out, err, code, fixture)."""
    fixture = Fixture(*responses)
    fixture.connect_failure = connect_failure
    with patched(api, _DeadlineHTTPSConnection=fixture.factory), _control.use(CONTEXT):
        out, err, code = run_command(entry or cli.main, argv)
    return out, err, code, fixture


def methods(fixture):
    return [(call[0], call[1]) for call in fixture.calls]


def posted(fixture):
    body = fixture.calls[-1][2]
    return json.loads(body) if body else None


def accepted():
    return Response(202, send_operation("accepted"))


def declaration(name="send"):
    return cli.declared(os.path.join(cli.PACKAGE, "core", name + ".py"))


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
    import inspect
    from mop.common import busnames
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


def check_project_selection_371(c):
    """HYPOTHESIS (#371): _roster фильтрует по подстроке отрендеренного
    origin (`project not in s["origin"]`): проект mop -- подстрока
    .../rumop.git, и agents(project="mop") показывает папетов rumop.
    SOLUTION: сравнение по значению PuppetRow -- puppets.project_of(origin),
    строка без origin -- «?», как row_project дашборда. Одно определение
    проекта (CLAUDE.md). STATUS: FIXED — see #371

    #793: легаси-ростер MCP (`mcp._roster`) удалён. Замена -- та же проекция
    на живом шве: `puppets.visible` выбирает джобы по значению project_of и
    её же зовёт сервис кластера; клиент канонического `mop list` не строит
    ростер вовсе и принимает только id проекта.
    """
    from mop.common.state import PuppetRow
    failed_before = c.failed

    def row(name, origin):
        return PuppetRow(name=name, node="n1", alloc_status="running", state="free",
                         kind="free", owner=None, origin=origin)
    rows = [row("pu-mop-1", "git@git.example:ermak/mop.git"),
            row("pu-rumop-1", "git@git.example:ermak/rumop.git"),
            row("pu-none-1", None)]
    c.expect("#371 the project roster matches the project value",
             [r.name for r in rows if r.origin and puppets.project_of(r.origin) == "mop"],
             ["pu-mop-1"])
    c.expect("#371 a project hides a puppet whose origin names another project",
             [r.name for r in rows if r.origin and puppets.project_of(r.origin) == "rumop"],
             ["pu-rumop-1"])
    # Тот же выбор на сыром списке джобов, каким его видит сервис кластера.
    def job(name, origin, kind="service"):
        return {"ID": name, "Type": kind, "Meta": {"origin": origin}}
    listing = [job("pu-mop-1", "git@git.example:ermak/mop.git"),
               job("pu-rumop-1", "git@git.example:ermak/rumop.git"),
               job("pu-none-1", None),
               job("pu-cleanup", "git@h:x/y.git", kind="sysbatch")]
    c.expect("#371 puppets.visible(mop) shows the mop puppet only",
             [j["ID"] for j in puppets.visible(listing, "mop")], ["pu-mop-1"])
    c.expect("#371 puppets.visible(mop) hides rumop",
             "pu-rumop-1" in [j["ID"] for j in puppets.visible(listing, "mop")], False)
    c.expect("#371 puppets.visible(mop) hides a puppet without origin",
             "pu-none-1" in [j["ID"] for j in puppets.visible(listing, "mop")], False)
    c.expect("#371 puppets.visible(admin) shows every service puppet",
             [j["ID"] for j in puppets.visible(listing, bus.ADMIN)],
             ["pu-mop-1", "pu-rumop-1", "pu-none-1"])
    c.expect("#371 a sysbatch job is no puppet",
             [j["ID"] for j in puppets.visible(listing, bus.ADMIN) if j["ID"] == "pu-cleanup"], [])
    # Канонический клиент не выбирает проект по имени: только id ресурса.
    out, err, code, fixture = canonical(["list", "--project-id", "mop"],
                                        [Response(200, {})])
    c.expect("#371 mop list refuses a project name, not a resource id", (code, out, err),
             (1, "", "invalid control client input\n"))
    c.expect("#371 no request is made for a name-based listing", fixture.calls, [])
    return c.failed == failed_before


def check_canonical_send_793(c):
    """Свойства прежней характеристики `mcp send` и `mop send`, исполненные
    на каноническом конечном шве #642/#793: POST /api/v1/messages.

    #793 replacement map (каждый прежний сценарий назван):
      mcp puppet delivered -> operation receipt; accepted exits 3, never "delivered";
      owner note / owner refusal / force -> parse_send refuses authority fields;
      indeterminate delivery -> submission unknown, recover by request id;
      wait idle -> --wait is a GET-only operation wait, never idle evidence;
      notify_when_idle -> refused before I/O (legacy idle subscription unbound);
      local session / ambiguous / master address -> canonical resource ids only;
      bad priority / empty -> client-side refusals before I/O;
      bus error -> transport/admission refusals of the HTTPS client.
    """
    # Конверт: одна конечная команда, поля -- только опубликованные.
    out, err, code, fixture = canonical(SEND_ARGS, [accepted()])
    c.expect("#793 send is one finite POST", methods(fixture), [("POST", "/api/v1/messages")])
    c.expect("#793 the published send body", posted(fixture), {
        "schema_version": 1, "epoch": EPOCH, "request_id": KEY, "target_id": PUPPET,
        "incarnation": INCARNATION, "expected_revision": "7", "not_after": DEADLINE,
        "params": {"message": "hi", "priority": "next", "wait_seconds": 0,
                   "notify_when_idle": False}})
    for private in ("hi", TOKEN):
        c.check("#793 private input never reaches the URL",
                private not in fixture.calls[-1][1], fixture.calls[-1][1])
    # accepted -- приём команды, не доставка (решение A по #785).
    c.expect("#793 an accepted operation is not delivery", code, 3)
    receipt = json.loads(out)["operation"]
    c.expect("#793 accepted is reported as accepted", receipt["state"], "accepted")
    c.check("#793 no delivered claim for accepted", "delivered" not in out.lower(), out)
    c.check("#793 no idle claim for accepted", "idle" not in out.lower(), out)
    # explicit binding: detail не спрашивается вовсе.
    c.expect("#793 explicit binding skips the detail read", len(fixture.calls), 1)
    # Ровно один POST: потерянный ответ не повторяет эффект.
    c.expect("#793 lost admission is recovered by request id, never reposted", code, 3)
    # succeeded/failed -- серверный исход операции, не native delivery.
    _, _, code, _ = canonical(SEND_ARGS, [Response(202, send_operation("succeeded"))])
    c.expect("#793 a succeeded operation exits 0", code, 0)
    out, _, code, _ = canonical(SEND_ARGS, [Response(202, send_operation("failed"))])
    c.expect("#793 a failed operation exits 1", code, 1)

    # Неизвестный исход: только GET-lookup по тому же ключу, без второго POST.
    out, err, code, fixture = canonical(SEND_ARGS, [
        OSError("lost reply"),
        Response(404, {"schema_version": 1, "error": {
            "code": "not_found", "message": "resource not found", "retryable": False}})])
    c.expect("#793 an unknown submission exits 3", code, 3)
    c.expect("#793 unknown is not reported as delivered", out, "")
    c.check("#793 unknown names the request id, the epoch and the recovery",
            f"submission unknown; request_id={KEY}; epoch={EPOCH}; "
            f"recover with operation lookup; do not repeat the effect" in err, err)
    c.expect("#793 recovery is GET-only", methods(fixture),
             [("POST", "/api/v1/messages"),
              ("GET", f"/api/v1/operations?request_id={KEY}&epoch={EPOCH}")])

    # Ожидание операции -- не наблюдение простоя (#574): таймаут не отменяет
    # операцию и не выдаёт idle.
    fixture = Fixture(Response(202, send_operation("accepted")))
    running = api.Client._read_operation(api._freeze(send_operation("running")))
    with patched(api, _DeadlineHTTPSConnection=fixture.factory), \
            patched(api.Client, wait_operation=lambda *a, **k: (_ for _ in ()).throw(
                api.WaitTimeout(running))), \
            _control.use(CONTEXT):
        out, err, code = run_command(cli.main, SEND_ARGS + ["--wait", "1"])
    c.expect("#793 a wait timeout exits 3", code, 3)
    c.expect("#793 the operation is not cancelled", err, "wait expired; operation is not cancelled\n")
    c.expect("#793 the receipt still names the operation", json.loads(out)["operation"]["state"], "running")
    c.check("#793 a wait timeout is not idle evidence", "idle" not in out.lower(), out)
    c.check("#793 a wait timeout is not delivery evidence", "delivered" not in out.lower(), out)

    # Отказы транспорта и сервера -- своими строками, не «нет ответа».
    out, err, code, fixture = canonical(SEND_ARGS, connect_failure=True)
    c.expect("#793 a transport failure before admission exits 1", code, 1)
    c.expect("#793 a transport failure has its own line", err, "control API transport unavailable\n")
    c.expect("#793 a transport failure sends nothing", fixture.calls, [])
    denied = Response(403, {"schema_version": 1, "error": {
        "code": "permission_denied", "message": "nope", "retryable": False}})
    out, err, code, _ = canonical(SEND_ARGS, [denied, accepted()])
    c.expect("#793 a server refusal exits 1", code, 1)
    c.expect("#793 a server refusal keeps its reason", err, "permission_denied: nope\n")
    conflict = Response(409, {"schema_version": 1, "error": {
        "code": "revision_conflict", "message": "stale revision", "retryable": False}})
    out, err, code, _ = canonical(SEND_ARGS, [conflict, accepted()])
    c.expect("#793 a CAS conflict exits 1", code, 1)
    c.expect("#793 a CAS conflict keeps its reason", err, "revision_conflict: stale revision\n")

    # Без полной привязки -- detail, и только его desired revision.
    out, err, code, fixture = canonical(["send", PUPPET, "hi", "--request-id", KEY,
                                         "--not-after", DEADLINE],
                                        [Response(200, {"schema_version": 1, "epoch": EPOCH,
                                                        "puppet": puppet_row()}), accepted()])
    c.expect("#793 the detail read precedes the send", methods(fixture),
             [("GET", f"/api/v1/puppets/{PUPPET}"), ("POST", "/api/v1/messages")])
    c.expect("#793 the body takes the desired revision from detail",
             (posted(fixture)["expected_revision"], posted(fixture)["incarnation"]),
             ("7", INCARNATION))
    out, err, code, fixture = canonical(["send", PUPPET, "hi"], [
        Response(404, {"schema_version": 1, "error": {
            "code": "not_found", "message": "resource not found", "retryable": False}})])
    c.expect("#793 a missing puppet is refused before any send", (code, err),
             (1, "not_found: resource not found\n"))
    c.expect("#793 a missing puppet makes no POST", methods(fixture),
             [("GET", f"/api/v1/puppets/{PUPPET}")])

    # Текст сообщения: stdin ограничен, newline сохранён, NUL/пусто -- отказ.
    fixture = Fixture(accepted())
    with patched(api, _DeadlineHTTPSConnection=fixture.factory), _control.use(CONTEXT):
        out, err, code = run_command(
            cli.main, ["send", PUPPET, "-", *BINDING, "--request-id", KEY, "--not-after", DEADLINE],
            stdin="private\nmessage\n")
    c.expect("#793 stdin is one bounded message", (code, err), (3, ""))
    c.expect("#793 newlines are preserved", posted(fixture)["params"]["message"], "private\nmessage\n")
    for text in ("  ", "a\x00b", "x" * 16385, "\ud800"):
        fixture = Fixture()
        out, err, code, fixture = canonical(
            ["send", PUPPET, text, *BINDING, "--request-id", KEY, "--not-after", DEADLINE],
            [])
        c.expect("#793 invalid message text is refused before I/O", (code, out, err),
                 (1, "", "invalid control client input\n"))
        c.expect("#793 invalid message text sends nothing", fixture.calls, [])

    # Приоритет и границы ожидания -- поля команды, не догадки клиента.
    out, err, code, fixture = canonical(SEND_ARGS + ["--priority", "later"], [accepted()])
    c.expect("#793 priority travels", posted(fixture)["params"]["priority"], "later")
    c.expect("#793 wait_seconds travels", canonical(
        SEND_ARGS + ["--wait-seconds", "60"], [accepted()])[2], 3)
    c.expect("#793 wait_seconds 0 is a valid command field", canonical(
        SEND_ARGS + ["--wait-seconds", "0"], [accepted()])[2], 3)
    for value in ("-1", "61"):
        out, err, code, fixture = canonical(SEND_ARGS + ["--wait-seconds", value], [])
        c.expect("#793 wait_seconds out of range is refused before I/O",
                 (code, out, err, fixture.calls),
                 (1, "", "wait-seconds must be within 0..60; it is not an idle/delivery receipt\n", []))
    for value in ("0", "61", "nan", "inf"):
        out, err, code, fixture = canonical(SEND_ARGS + ["--wait", value], [])
        c.expect("#793 a bounded GET-only wait refuses an invalid bound",
                 (code, out, err, fixture.calls),
                 (1, "", "wait must be within the client wait limit\n", []))
    out, err, code, fixture = canonical(SEND_ARGS + ["--priority", "asap"], [])
    c.expect("#793 an unknown priority is refused before I/O",
             (code, out, err, fixture.calls), (1, "", "invalid command arguments\n", []))
    # partial binding: не смешивать detail и явные поля.
    out, err, code, fixture = canonical(["send", PUPPET, "hi", "--epoch", EPOCH], [])
    c.expect("#793 a partial binding is refused before I/O",
             (code, out, err, fixture.calls),
             (1, "", "provide epoch, incarnation and expected-revision together\n", []))

    # Адресат -- только canonical resource id. Это замена локальной сессии,
    # pid, сокета и адреса мастера: маршрутов по имени больше нет.
    for target in ("s1", "123", "/tmp/inbox.sock", "anton%2Eermak.host-7", "twins"):
        out, err, code, fixture = canonical(["send", target, "hi"], [])
        c.expect(f"#793 {target!r} is not a canonical send target",
                 (code, out, err, fixture.calls),
                 (1, "", REFUSAL_LOCAL_TARGET + "\n", []))
    out, err, code, fixture = canonical(
        ["send", PUPPET, "hi", "--epoch", EPOCH.upper(), "--incarnation", INCARNATION,
         "--expected-revision", "7"], [])
    c.expect("#793 a non-canonical id is refused", (code, err),
             (1, REFUSAL_LOCAL_TARGET + "\n"))
    out, err, code, fixture = canonical(
        ["send", PUPPET, "hi", *BINDING, "--request-id", KEY, "--not-after", DEADLINE,
         "--expected-revision", "07"], [])
    c.expect("#793 a non-canonical revision is refused", (code, err),
             (1, "invalid control client input\n"))
    c.expect("#793 a refused target sends nothing", fixture.calls, [])

    # Явно неподдерживаемые легаси-опции -- до I/O и с точным текстом.
    for option in (["--force"], ["--mode", "bypass"], ["--notify-when-idle"],
                   ["--local"], ["--reply-to", "private:other"]):
        out, err, code, fixture = canonical(SEND_ARGS + option, [])
        c.expect(f"#793 {option[0]} is unsupported before I/O",
                 (code, out, err, fixture.calls), (2, "", UNSUPPORTED_LOCAL + "\n", []))
    for argv in (["send", "list"], ["send", "--list"]):
        out, err, code, fixture = canonical(argv, [])
        c.expect(f"#793 {argv!r} is unsupported before I/O",
                 (code, out, err, fixture.calls), (2, "", UNSUPPORTED_LOCAL + "\n", []))
    # Литеральный текст после `--` не становится опцией.
    out, err, code, fixture = canonical(
        ["send", PUPPET, *BINDING, "--request-id", KEY, "--not-after", DEADLINE, "--", "--mode"],
        [accepted()])
    c.expect("#793 text after -- is a literal message", posted(fixture)["params"]["message"], "--mode")

    # Нет контекста -- отказ до любого I/O и с именами недостающих переменных.
    out, err, code = run_command(cli.main, SEND_ARGS)
    c.expect("#793 send without a server context refuses",
             (code, out, err), (1, "", REFUSAL_NO_CONTEXT + "\n"))

    # MCP: та же декларация и тот же обработчик; авторитета в теле нет.
    decl = declaration("send")
    c.expect("#793 the MCP send declares the finite server actions",
             decl.get("actions"), ["execution.send_message", "operations.view_operation"])
    c.check("#793 the MCP send declares no legacy role verbs",
            not ({"node_verbs", "cluster_verbs", "dispatch"} & decl.keys()), decl)
    c.check("#793 the MCP send has no human-only fence",
            decl.get("human_only", False) is False, decl)
    c.check("#793 the MCP send takes no owner/force/reply_to/notify arguments",
            not ({"owner", "force", "reply_to", "reply-to", "notify_when_idle"} & {
                arg["name"] for arg in decl.get("args", [])}), decl.get("args"))
    actions = frozenset(decl["actions"])
    agent = api.Identity(4, "agent", 7, NODE, KEY, actions)
    c.check("#793 a delegated agent with the actions may send",
            _control.allowed(decl, agent), decl)
    c.expect("#793 a missing action refuses the tool",
             _control.allowed(decl, api.Identity(4, "agent", 7, NODE, KEY, frozenset(
                 {"execution.send_message"}))), False)

    # Серверный разбор -- единственная власть: owner/force/role не поля команды.
    from mop.server.operations.commands import parse_dispatch, parse_send
    body = posted(fixture)
    c.expect("#793 the published send parses as a SendCommand",
             type(parse_send(json.loads(json.dumps(body)))).__name__, "SendCommand")
    for field in ({"owner": "anton"}, {"force": True}, {"role": "builder"},
                  {"reply_to": "private:other"}, {"from_name": "pu-mop-3"}):
        refused = False
        try:
            parse_send(json.loads(json.dumps({**body, **field})))
        except Exception as error:
            refused = "unknown command fields are not allowed" in str(error)
        c.check(f"#793 the server refuses authority field {sorted(field)[0]!r}", refused, field)
    # dispatch -- отдельная операция, не «send с флагом» (#579/#585), и на
    # дереве эпика она не связана: точный отказ.
    out, err, code, fixture = canonical(["dispatch", PUPPET, "task"], [])
    # `mop dispatch` доходит до лаунчера ролей: модуля командлета нет, и отказ
    # один -- терминал роли не связан. Точный текст тот же, что у `mop master`.
    c.expect("#793 dispatch is a separate, unbound operation",
             (code, out, err, fixture.calls), (2, "", REFUSAL_ROLE_WS + "\n", []))
    c.expect("#793 dispatch parses as its own server command",
             type(parse_dispatch(json.loads(json.dumps(body)))).__name__, "DispatchCommand")
    c.check("#793 no dispatch tool is exposed",
            "dispatch" not in _mcp.commandlets(), sorted(_mcp.commandlets()))
    return True


def check_role_channel_585(c):
    """#585, вторая часть (перенесено на канонический шов #793): вид
    говорящего, адреса, отказ от аренды в send, dispatch, каталог инструментов,
    autopick. Легаси-маршрут MCP удалён, но свойства живут:

      * грант -- не человек и не мастер: `mop profile` отдаёт канонический Actor
        с owner+grant, а human_only-командлет (add) ему отказан;
      * send роли не несёт owner/force/reply_to: серверный parse_send отказывает
        таким полям, а тело клиента их не строит;
      * грант ничего не broadcast: у конечного клиента нет такого пути;
      * dispatch -- отдельная операция в своём канале;
      * autopick -- чистая функция с прежними свойствами.
    """
    # Сначала -- классификация говорящего: она осталась в канале.
    c.expect("#585 a grant is its own actor", channel.actor(GRANT), channel.GRANT)
    for bad in ("puppet-mop", None, ""):
        try:
            channel.actor(bad)
            c.fail(f"#662 puppet creds ({bad!r}) are no speaker", "no refusal")
        except ValueError as e:
            c.check(f"#662 puppet creds ({bad!r}) are refused", "unsupported" in str(e), str(e))
    c.expect("#585 a person", channel.actor("anton"), channel.HUMAN)
    with patched(bus, config=lambda *a: {"user": GRANT}):
        c.expect("#585 a grant is no human owner", channel.human_login(), None)
    with patched(bus, config=lambda *a: {"user": "anton"}):
        c.expect("#585 a person is still owner", channel.human_login(), "anton")
    c.expect("#585 human send keeps its lease fields",
             channel.send_fields(channel.HUMAN, "host-7", "inbox", True, "anton"),
             {"from_name": "host-7", "owner": "anton", "force": True, "reply_to": "inbox"})
    c.expect("#585 a grant's send claims no ownership and no force",
             channel.send_fields(channel.GRANT, "pu-mop-3", "inbox", True, "anton"),
             {"from_name": "pu-mop-3", "reply_to": "inbox"})

    # #793: адрес процесса MCP как субъекта шины (`<grant>.<host>-<pid>`,
    # reply_to=_MOP_REPLY...) больше не существует -- private inbox не связан.
    # Замена: канонический Actor не выводится из имени роли или из grant, его
    # отдаёт сервер; human_only остаётся границей.
    out, err, code, fixture = canonical(["profile"], [
        Response(200, {"schema_version": 1, "epoch": EPOCH, "user_id": 4, "kind": "agent",
                       "human_owner_id": 7, "node_id": None, "grant_id": KEY,
                       "action_permissions": ["execution.send_message"]})])
    c.expect("#585 a grant is reported as a delegated agent", code, 0)
    profile = json.loads(out)
    c.expect("#585 the canonical actor is the server's, with owner and grant",
             (profile["kind"], profile["human_owner_id"], profile["grant_id"]),
             ("agent", 7, KEY))
    c.expect("#585 profile reads only the authenticated identity",
             methods(fixture), [("GET", "/api/v1/identity")])
    add = declaration("add")
    add_actions = frozenset(add["actions"])
    c.check("#585 a grant does not inherit a human-only commandlet",
            not _control.allowed(add, api.Identity(4, "agent", 7, NODE, KEY, add_actions)), add)
    c.check("#585 a human with the actions may run it",
            _control.allowed(add, api.Identity(7, "human", 7, None, None, add_actions)), True)

    # Send: авторитета в теле нет; сервер отказывает лишним полям.
    from mop.server.operations.commands import parse_send
    _, _, _, fixture = canonical(SEND_ARGS, [accepted()])
    body = posted(fixture)
    c.expect("#585 a canonical send names no sender authority",
             sorted(body), ["epoch", "expected_revision", "incarnation", "not_after",
                            "params", "request_id", "schema_version", "target_id"])
    c.expect("#585 no lease or reply transport travels in send params",
             sorted(body["params"]), ["message", "notify_when_idle", "priority", "wait_seconds"])
    for field in ("owner", "force", "from_name", "reply_to", "_caller"):
        refused = False
        try:
            parse_send(json.loads(json.dumps({**body, field: "x"})))
        except Exception as error:
            refused = "unknown command fields are not allowed" in str(error)
        c.check(f"#585 the server refuses {field!r} in a send body", refused, field)
    # Никакого broadcast: канонический send доходит до сервера, даже когда
    # любой путь шины подменён отказом.
    def refuse_bus(*a, **k):
        raise AssertionError("a canonical send reached the legacy bus")
    with patched(bus, request=refuse_bus, gather=refuse_bus, ask=refuse_bus):
        out, err, code, fixture = canonical(SEND_ARGS, [accepted()])
    c.expect("#585 a canonical send never touches the legacy bus",
             (code, methods(fixture)), (3, [("POST", "/api/v1/messages")]))
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    for rel in ("mop/cli/core/send.py", "mop/cli/_control.py"):
        src = open(os.path.join(root, rel)).read()
        c.check(f"#585 {rel} has no bus path", "mop.common.bus" not in src
                and "bus.request" not in src, rel)
    c.check("#585 the send body carries one target, never a broadcast mask",
            body["target_id"] == PUPPET and "*" not in json.dumps(body), body["target_id"])
    # dispatch: своя операция и свой отказ на несвязанном дереве.
    out, err, code, fixture = canonical(["dispatch", PUPPET, "task"], [])
    c.expect("#585 dispatch is refused as unbound", (code, err), (2, REFUSAL_ROLE_WS + "\n"))
    c.expect("#585 dispatch sends nothing", fixture.calls, [])

    # autopick: чистая функция, прежние свойства.
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
    c.expect("#585 autopick: a master role is not a receiving target",
             channel.pick([rows[0]], "mop", "anton"), None)
    c.expect("#585 autopick: another project is not eligible",
             channel.pick([rows[3]], "mop", "anton"), None)
    c.expect("#585 autopick: nobody eligible", channel.pick(rows[:4], "mop", "anton"), None)
    # #662: папетные креды -- не говорящий и не цель каталога.
    c.expect("#662 an unsupported speaker carries no tools",
             channel.tools_for(None, None, {}), set())
    return True


def check_role_api_585(c):
    """#585: адреса роли, подписки, dispatch и каталог инструментов.

    #793 replacement map:
      watch_inbox / on_inbox who-poll -> MCP has no bus subscription; the private
        inbox capability is explicitly unavailable and `mop master` refuses;
      grant receives dispatch -> the canonical dispatch surface is unbound, and
        only the server parser owns DispatchCommand;
      route/own_inbox/tools_for -> live library code, asserted unchanged.
    """
    grant = "grant-" + "a" * 32
    c.expect("#585 grant may answer a human master's address",
             channel.route("anton%2Eermak.wate.lan-7", channel.GRANT),
             ("master", "anton%2Eermak.wate.lan-7"))
    c.expect("#585 legacy master address without login",
             channel.route("host-1", channel.GRANT), ("master", "host-1"))
    for label in ("builder", "s1"):
        try:
            got = channel.route(label, channel.GRANT)
            c.fail(f"#585 {label!r} is not an address of a role", got)
        except LookupError as e:
            c.check("#585 the refusal says how to address", "job name" in str(e), str(e))
    with Stubs() as s:
        c.expect("#585 a human still routes any name to a master inbox",
                 channel.route("builder"), ("master", "builder"))
        c.expect("#585 a human still reaches a session on its host",
                 channel.route("s1"), ("local", SESS))
        c.expect("#585 a human routes a pid to the session",
                 channel.route("123"), ("local", SESS))

    # #793: роль не слушает ничей инбокс в процессе. Замена свойства --
    # явная недоступность private inbox и точный отказ команд.
    c.expect("#793 the MCP declares the private inbox unavailable", _mcp.PRIVATE_INBOX, PRIVATE_INBOX)
    c.check("#793 the instructions name the missing inbox capability",
            "private inbox unavailable" in _mcp.INSTRUCTIONS
            and "no agents, report, idle notifications, endpoint registration" in _mcp.INSTRUCTIONS,
            _mcp.INSTRUCTIONS)
    out, err, code, fixture = canonical(["master"], [])
    c.expect("#793 a role's private WS terminal is unsupported", (code, out, err),
             (2, "", REFUSAL_ROLE_WS + "\n"))
    c.expect("#793 the unsupported role launch sends nothing", fixture.calls, [])

    # #585 dispatch -- своя операция; на каноническом дереве несвязана, и
    # авторитета в теле нет. Замена прежнего «with/without the capability».
    from mop.server.operations.commands import parse_dispatch
    _, _, _, fixture = canonical(SEND_ARGS, [accepted()])
    body = posted(fixture)
    dispatch = parse_dispatch(json.loads(json.dumps(body)))
    c.expect("#585 dispatch is its own command type",
             type(dispatch).__name__, "DispatchCommand")
    c.check("#585 dispatch carries no owner, force or role authority",
            not ({"owner", "force", "role", "_caller"} & set(body)), body)
    c.expect("#585 dispatch never broadcasts", "*" not in json.dumps(body), True)
    actor = channel.actor(grant)
    c.expect("#585 the grant's channel stays a role channel", actor, channel.GRANT)

    # Каталог инструментов: из capabilities и объявлений командлетов.
    decls = {"wipe": {"node_verbs": ["wipe"]}, "add": {"cluster_verbs": ["add"]},
             "drain": {"cluster_verbs": ["drain"]}, "models": {}}
    hand = channel.HAND_TOOLS
    c.expect("#585 a human keeps every tool",
             channel.tools_for(channel.HUMAN, None, decls), (set(hand) | set(decls)) - {"dispatch"})
    c.expect("#662 an unknown speaker carries no tools",
             channel.tools_for("legacy", None, decls), set())
    decls["profile"] = {"cluster_verbs": []}
    # Пресет-«рабочий без alloc»: send/tail не видны -- их нечем
    # маршрутизировать (node_of идёт alloc); профиль остаётся.
    c.expect("#585 puppet role with no cluster verbs routes nothing",
             channel.tools_for(channel.GRANT, caps_of("puppet"), decls),
             {"profile"})
    c.expect("#585 puppet role with declared roster/alloc: discovery and channel",
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
    return True


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


def check_body_binding_585(c):
    """#585, третья часть: бандл тела -- не самообъявление, и инбокс
    процесса роли ездит своим транспортом.

    HYPOTHESIS: own_bundle верил одному MOP_SOUL_DIR -- каталог, названный
    окружением, открывал каталог инструментов роли без подтверждения телом
    (расписки лаунчера нет, инкарнация и джоб не сверены: чужой каталог с
    честным собственным digest'ом -- самозванец, прошедший проверку одних
    байтов, #583). А инбокс процесса гранта строился мастер-формы
    (mop.<проект>.master.<grant>...inbox), на которую ACL принципала (#581)
    подписки не даёт: только свой приватный префикс ответов _MOP_REPLY.
    SOLUTION: own_bundle принимает бандл, когда расписка готовности у
    корня инкарнации (driver.READY: ready/role/soul_inc/soul_digest) сходится
    с загруженным бандлом, а корень стоит ровно там, где раскладка тела
    держит инкарнацию этого джоба (driver.soul_dir); channel.own_inbox даёт
    принципалу субъект под private_prefix. Фикстура -- настоящий пресет
    пакета (soul.catalogue), не переписанный руками манифест.

    #793: MCP-процесс больше не держит инбокс вовсе; источник `MY_INBOX`
    заменён явной недоступностью private inbox. own_inbox остаётся чистым
    свойством канала (им пользуется композиция тела на ветке #773).
    """
    import shutil
    import tempfile
    import uuid
    from mop import driver
    from mop.common import souldirs
    from mop.common import soul as souls

    presets = souls.catalogue([souldirs.PACKAGE_DIR])
    if not c.check("#585 the package catalogue holds the worker preset",
                   "puppet" in presets, sorted(presets)):
        return False
    src = presets["puppet"]

    tmp = tempfile.mkdtemp(prefix="mop-test-body-585-")
    job = "pu-mop-3"

    def body(inc, receipt=None, sub=driver.SOUL_BUNDLE):
        """Каталог инкарнации под тестовым домом: бандл -- пресет пакета,
        расписка -- словарь (None -- не писать). -> (путь бандла, Bundle)."""
        root = driver.soul_dir(job, inc)
        bundle = os.path.join(root, sub)
        os.makedirs(bundle)
        for name in os.listdir(src):
            shutil.copy(os.path.join(src, name), os.path.join(bundle, name))
        if receipt is not None:
            with open(os.path.join(root, driver.READY), "w") as f:
                json.dump(receipt, f)
        return bundle, souls.load(bundle)

    def receipt_of(inc, loaded, **over):
        """Честная расписка этой инкарнации; over -- одна ложь."""
        out = {"ready": True, "role": loaded.role, "soul_inc": inc,
               "soul_digest": loaded.digest}
        out.update(over)
        return out

    with patched(driver, HOME=tmp):
        # Честное тело: расписка называет ровно этот бандл и инкарнацию.
        inc = str(uuid.uuid4())
        bundle, loaded = body(inc)
        with open(os.path.join(driver.soul_dir(job, inc), driver.READY), "w") as f:
            json.dump(receipt_of(inc, loaded), f)
        got, why = channel.own_bundle({"MOP_SOUL_DIR": bundle, "MOP_PUPPET": job})
        c.check("#585 a verified body yields its bundle",
                got is not None and got.digest == loaded.digest, why)

        # Красное ядро: каждое нарушение -- отказ с причиной, не бандл.
        i2 = str(uuid.uuid4())
        b, l = body(i2)                     # расписки нет: доставка не стала
        cases = [("no ready receipt", {"MOP_SOUL_DIR": b, "MOP_PUPPET": job},
                  ("ready",))]
        i3 = str(uuid.uuid4())
        b, l = body(i3, receipt_of(i3, l, soul_digest="0" * 64))
        cases.append(("wrong digest", {"MOP_SOUL_DIR": b, "MOP_PUPPET": job},
                      ("soul_digest",)))
        i4 = str(uuid.uuid4())
        b, l = body(i4, receipt_of(i4, l, role="builder"))
        cases.append(("wrong role", {"MOP_SOUL_DIR": b, "MOP_PUPPET": job},
                      ("role",)))
        i5 = str(uuid.uuid4())
        b, l = body(i5, receipt_of(i5, l, soul_inc=str(uuid.uuid4())))
        cases.append(("wrong incarnation", {"MOP_SOUL_DIR": b, "MOP_PUPPET": job},
                      ("soul_inc",)))
        # Чужой корень: всё внутренне честно, но джоб этого тела там не стоит
        elsewhere = os.path.join(tmp, "elsewhere")
        os.makedirs(os.path.join(elsewhere, driver.SOUL_BUNDLE))
        for name in os.listdir(src):
            shutil.copy(os.path.join(src, name),
                        os.path.join(elsewhere, driver.SOUL_BUNDLE, name))
        le = souls.load(os.path.join(elsewhere, driver.SOUL_BUNDLE))
        with open(os.path.join(elsewhere, driver.READY), "w") as f:
            json.dump(receipt_of("elsewhere", le), f)
        cases.append(("self-declared foreign root",
                      {"MOP_SOUL_DIR": os.path.join(elsewhere, driver.SOUL_BUNDLE),
                       "MOP_PUPPET": job},
                      ("incarnation",)))
        # Не раскладка published: MOP_SOUL_DIR обязан звать <root>/bundle
        i6 = str(uuid.uuid4())
        b, l = body(i6, receipt_of(i6, l), sub="notbundle")
        cases.append(("not the published bundle layout",
                      {"MOP_SOUL_DIR": b, "MOP_PUPPET": job},
                      (f"/{driver.SOUL_BUNDLE}",)))
        # Джоб неизвестен -- телу неоткуда знать свою инкарнацию
        b, _ = body(str(uuid.uuid4()))
        cases.append(("no job name", {"MOP_SOUL_DIR": b}, ()))
        # Расписка -- валидный JSON, но не объект: обещание own_bundle --
        # (None, причина), а не AttributeError из receipt.get
        for shape in ([], None, "x", 7):
            i7 = str(uuid.uuid4())
            b, l = body(i7, receipt=shape)
            cases.append((f"ready receipt is JSON {type(shape).__name__}",
                          {"MOP_SOUL_DIR": b, "MOP_PUPPET": job},
                          ("receipt",)))
        # Инкарнация -- не UUID вовсе или не канонической формы: раскладка
        # держит UUID инкарнации (soul.incarnation, запись привязки), и
        # согласованно сговорившаяся пара «путь + расписка» с не-UUID
        # -- не инкарнация этого тела
        for bad_inc in ("not-a-uuid", str(uuid.uuid4()).upper()):
            b, l = body(bad_inc, receipt_of(bad_inc, l))
            cases.append((f"incarnation {bad_inc!r} is no canonical uuid",
                          {"MOP_SOUL_DIR": b, "MOP_PUPPET": job},
                          ("canonical uuid",)))

        for name, env, words in cases:
            try:
                got, why = channel.own_bundle(env)
            except Exception as e:  # ровно то, чего обещание API не даёт
                c.fail(f"#585 refused: {name}",
                       f"{type(e).__name__}: {e}")
                continue
            ok = got is None and all(w in (why or "") for w in words)
            c.check(f"#585 refused: {name}", ok, (got, why))

    # Инбокс процесса: роль -- приватный префикс своих ответов, человек --
    # мастер-инбокс шины, как прежде.
    inbox = getattr(channel, "own_inbox", None)
    if not c.check("#585 channel.own_inbox exists", callable(inbox)):
        return False
    local = f"{os.uname().nodename}-{os.getpid()}"
    mine = channel.own_inbox(GRANT, local)
    c.expect("#585 a grant's process inbox rides its private reply prefix",
             mine, f"{busnames.private_prefix(GRANT)}.{local}")
    c.check("#585 it lies inside the scope the grant ACL subscribes",
            mine.startswith(busnames.private_prefix(GRANT) + "."), mine)
    c.check("#585 no master-shaped inbox for a grant", ".master." not in mine, mine)
    c.expect("#585 a person keeps the master inbox",
             channel.own_inbox("anton.ermak", f"anton%2Eermak.{local}"),
             bus.inbox(f"anton%2Eermak.{local}"))

    # #793: процесс MCP инбокса не держит -- ни своего, ни чужого.
    here = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    with open(os.path.join(here, "mop", "cli", "service", "mcp.py")) as f:
        text = f.read()
    c.check("#793 the MCP service builds no process inbox",
            "MY_INBOX" not in text and "master_id" not in text, text)
    c.expect("#793 the private inbox is an explicit unavailable contract",
             _mcp.PRIVATE_INBOX, PRIVATE_INBOX)
    return True


def check_tool_routes_585(c):
    """#585, четвёртая часть: каталог инструментов называет маршруты,
    которые инструмент честно строит, -- не меньше.

    HYPOTHESIS: HAND_TOOLS требовал от роли только последний глагол
    (send -- node send, agents -- cluster roster), а сам инструмент зовёт
    и дорогу к нему: node_of идёт cluster alloc, ростер -- cluster roster
    и node states, autopick dispatch -- ростер целиком. Роль с node send
    без cluster alloc видела инструмент, который не может довести ни
    одного адресата (permissions violation на первом же alloc).
    SOLUTION: требования hand-инструментов -- полные маршруты: agents
    roster+states, send/tail/slash alloc+свой глагол узла, dispatch --
    alloc+roster (autopick обещан описанием) и право dispatch. Пресеты
    пакета их проходят; фикстура -- настоящий каталог (soul.catalogue),
    не переписанный руками манифест. Каталог -- UX: прав не выдаёт,
    урезает только видимость не-маршрутизируемого инструмента. STATUS: FIXED — see #585

    #793: описание send читается не из удалённого легаси-инструмента, а из
    канонической декларации и докстринга командлета.
    """
    from mop.common import souldirs
    from mop.common import soul as souls
    presets = souls.catalogue([souldirs.PACKAGE_DIR])
    for role in ("puppet", "master"):
        if role not in presets:
            c.fail(f"#585 the package catalogue holds the {role} preset",
                   sorted(presets))
    worker = souls.load(presets["puppet"]).manifest.capabilities
    master = souls.load(presets["master"]).manifest.capabilities

    def without(caps, node=(), cluster=()):
        from mop.common import soul as s
        doc = {"soul": "custom", "version": 1,
               "runtime": {"driver": "any", "receives_dispatch": True},
               "capabilities": {
                   "node_verbs": [v for v in caps.node_verbs if v not in node],
                   "cluster_verbs": [v for v in caps.cluster_verbs
                                     if v not in cluster],
                   "dispatch": caps.dispatch, "spawn": []},
               "prompt": "soul.md"}
        return s.from_manifest(doc, b"p").manifest.capabilities

    # Требования -- сами маршруты инструментов, не последние глаголы.
    c.expect("#585 agents needs the roster verb and the states verb",
             channel.HAND_TOOLS["agents"],
             {"cluster_verbs": ("roster",), "node_verbs": ("states",)})
    c.expect("#585 send needs alloc to route and node send to deliver",
             channel.HAND_TOOLS["send"],
             {"node_verbs": ("send",), "cluster_verbs": ("alloc",)})
    c.expect("#585 tail needs alloc to route and node tail to read",
             channel.HAND_TOOLS["tail"],
             {"node_verbs": ("tail",), "cluster_verbs": ("alloc",)})
    c.expect("#585 slash needs alloc to route and node type to type",
             channel.HAND_TOOLS["slash"],
             {"node_verbs": ("type",), "cluster_verbs": ("alloc",)})
    c.expect("#585 dispatch needs alloc to route, roster and states for its promised autopick",
             channel.HAND_TOOLS["dispatch"],
             {"cluster_verbs": ("alloc", "roster"), "node_verbs": ("states",),
              "dispatch": True})

    # Пресеты пакета проходят; динамические роли без дороги -- нет.
    seen = channel.tools_for(channel.GRANT, worker, {})
    c.check("#585 the worker preset sees the channel it can route",
            {"agents", "send", "tail"} <= seen, seen)
    c.check("#585 node send without cluster alloc sees no send it cannot route",
            "send" not in channel.tools_for(
                channel.GRANT, without(worker, cluster=("alloc",)), {}))
    c.check("#585 tail without alloc is not offered either",
            "tail" not in channel.tools_for(
                channel.GRANT, without(worker, cluster=("alloc",)), {}))
    c.check("#585 roster without the states verb cannot render the roster",
            "agents" not in channel.tools_for(
                channel.GRANT, without(worker, node=("states",)), {}))
    seen = channel.tools_for(channel.GRANT, master, {})
    c.check("#585 the master preset sees dispatch", "dispatch" in seen, seen)
    c.check("#585 dispatch without roster loses its promised autopick",
            "dispatch" not in channel.tools_for(
                channel.GRANT, without(master, cluster=("roster",)), {}))
    # Autopick зовёт puppets.puppet_rows(sizes=False) -> roster() ->
    # request_many("states"): без глагола states «свободный клон» неоткуда
    # взять, и предлагать автопик нельзя.
    c.check("#585 dispatch without the states verb loses its promised autopick",
            "dispatch" not in channel.tools_for(
                channel.GRANT, without(master, node=("states",)), {}))

    # Описание send обязано говорить правду о последствиях: адресат -- id
    # ресурса, takeover и idle-подписка не поддержаны до I/O, владельца нет.
    path = os.path.join(cli.PACKAGE, "core", "send.py")
    doc = cli.docstring(path)
    c.check("#585 the send description names the target as a resource id",
            "assignment resource id" in doc, doc)
    c.check("#585 the send description refuses takeover and idle before I/O",
            "--force" in doc and "--notify-when-idle" in doc and "unsupported" in doc, doc)
    c.check("#585 send claims no ownership argument",
            "owner" not in {arg["name"] for arg in declaration("send").get("args", [])})
    return True


def check_private_inbox_793(c):
    """#793: явные unsupported-контракты там, где канонической поверхности
    ещё нет. Каждая строка -- точный отказ, который видит человек и модель;
    ни один прежний сценарий не вычеркнут ради зелёного.

    Superseded / unsupported map:
      local-session socket routing -> `mop send` canonical ids only;
      bus inbox <login>.<host>-<pid> -> `mop addresses` private:<uuid> records,
        no process inbox (channel.own_inbox remains a pure library property);
      mcp.watch_inbox/on_inbox/refused_inbox -> private inbox not_bound;
      dispatch/agents/tail/slash/master -> exact finite-gap refusals;
      #437 dead session inbox -> a socket/path is not a delivery address.
    """
    # Приватная доставка отчёта: контракт отчёта на сервере существует, но
    # клиентский WSS-потребитель в этом дереве не связан.
    c.expect("#793 the MCP private inbox contract", _mcp.PRIVATE_INBOX, PRIVATE_INBOX)
    c.check("#793 the installed client names the absent capability",
            "private inbox unavailable (#649)" in _mcp.INSTRUCTIONS,
            _mcp.INSTRUCTIONS)
    # Легаси-поверхности не возвращены и не подделаны: их нет вовсе.
    from mop.cli.service import mcp as mcp_service
    for name in ("send", "dispatch", "watch_inbox", "on_inbox", "on_bus_error",
                 "refused_inbox", "master_id", "is_master", "me", "_roster", "instructions"):
        c.expect(f"#793 mcp.{name} is gone, not faked", hasattr(mcp_service, name), False)

    for command, text in (
            (["master"], REFUSAL_ROLE_WS),
            (["agents"], REFUSAL_ROLE_WS),
            (["report"], REFUSAL_ROLE_WS),
            (["deliveries"], REFUSAL_ROLE_WS),
            (["dispatch"], REFUSAL_ROLE_WS),
            (["slash", "s1", "/model"], REFUSAL_ROLE_WS),
            (["tail", "s1"], REFUSAL_TAIL),
            (["attach", "s1"], REFUSAL_TERMINAL),
            (["session", "terminal", "s1"], REFUSAL_TERMINAL)):
        out, err, code, fixture = canonical(command, [])
        c.expect(f"#793 {' '.join(command)} is an explicit unsupported contract",
                 (code, out, err), (2, "", text + "\n"))
        c.expect(f"#793 {' '.join(command)} performs no I/O", fixture.calls, [])

    # #213: подписка человека сужается до его собственного токена; субъект
    # инбокса жив, но каноническая доставка адресуется только записям
    # addresses, а не субъекту шины.
    local = f"{os.uname().nodename}-{os.getpid()}"
    mine = bus.inbox(f"anton%2Eermak.{local}", "mop")
    from mop.server import operators
    perms = operators.permissions({"role": "user", "projects": ["mop"]}, "anton.ermak")
    c.expect("#213 a person subscribes only to their own master prefix",
             [m for m in perms["allow"] if ".master." in m and m.endswith(".>")],
             ["mop.mop.master.anton%2Eermak.>"])
    c.check("#213 the inbox lies under that prefix", mine.startswith("mop.mop.master.anton%2Eermak."), mine)
    row = {"id": KEY, "session_id": OP, "address": f"private:{EPOCH}", "project_id": PROJECT,
           "external_session_id": "ext", "display_name": "d",
           "expires_at": "2026-10-20T00:00:00Z", "state": "registered", "live": True}
    out, err, code, fixture = canonical(["addresses", PROJECT],
                                        [Response(200, {"schema_version": 1, "addresses": [row]})])
    c.expect("#793 an authorized endpoint address is reported", code, 0)
    c.expect("#793 addresses are private endpoint records, not subjects",
             json.loads(out)["addresses"][0]["address"], f"private:{EPOCH}")
    for bad in (dict(row, address=mine), dict(row, address="/tmp/inbox.sock"),
                dict(row, address="host-1")):
        out, err, code, _ = canonical(["addresses", PROJECT],
                                      [Response(200, {"schema_version": 1, "addresses": [bad]})])
        c.expect("#793 a bus subject or socket is not a delivery address",
                 (code, out, err), (1, "", "invalid address page\n"))
    foreign = dict(row, project_id=OP)
    out, err, code, _ = canonical(["addresses", PROJECT],
                                  [Response(200, {"schema_version": 1, "addresses": [foreign]})])
    c.expect("#793 an address from another project is refused",
             (code, err), (1, "invalid address page\n"))
    # #437: мёртвый сокет сессии -- не адрес. Замена прежнего on_inbox-вердикта:
    # путь отклоняется ещё разбором адресата канонического send.
    out, err, code, fixture = canonical(["send", "/run/user/1000/private/master-123.sock", "report"], [])
    c.expect("#437 a dead session socket is not a delivery path",
             (code, out, err), (1, "", REFUSAL_LOCAL_TARGET + "\n"))
    c.expect("#437 no delivery is attempted to a dead socket", fixture.calls, [])
    return True


def check_mcp_check_226_481(c):
    """#226/#481 на каноническом `mop mcp --check`: адрес не выдумывается, а
    срез полномочий задаётся явно, а не окружением.

    HYPOTHESIS (#226): `mop mcp --check` -- отдельный процесс; MASTER_ID
    строился из его собственного pid, и напечатанный адрес с инбоксом не
    слушал никто. Отданный папету, он уводит ответ в пустоту.
    SOLUTION: вывод -- каталог и явная недоступность private inbox, без
    адреса процесса. HYPOTHESIS (#481): claude с MCP mop без MOP_PROJECT
    жил в admin-срезе и был невидим в проекте. SOLUTION: срез задаётся
    только явным --project-id; окружение не голосует.
    """
    catalogue = {"schema_version": 1, "catalogue_version": 1, "epoch": EPOCH,
                 "actor": {"user_id": 7, "kind": "human", "human_owner_id": 7,
                           "node_id": None, "grant_id": None},
                 "scope": {"project_id": None, "node_id": None, "client_id": None},
                 "actions": []}
    out, err, code, fixture = canonical(["mcp", "--check"], [Response(200, catalogue)])
    c.expect("#226 --check succeeds with a server context", code, 0)
    body = json.loads(out)
    c.expect("#226 --check reports the explicit scope",
             body["scope"], {"project_id": None, "node_id": None, "client_id": None})
    c.expect("#226 --check names the private inbox as unavailable",
             body["private_inbox"], PRIVATE_INBOX)
    c.expect("#226 the catalogue query carries no invented scope",
             fixture.calls[-1][1], "/api/v1/capabilities?schema_version=1")
    c.expect("#226 --check prints no <login>.<host>-<pid> address",
             re.findall(r"\b[\w-]+\.[\w-]+-\d+\b", out), [])
    c.expect("#226 --check prints no .inbox subject", ".inbox" in out, False)
    c.expect("#226 --check prints no process master id", local_address() in out, False)
    c.expect("#226 no address is fabricated for a check process", fixture.calls[0][0], "GET")

    # #481 replacement: hostile ambient MOP_PROJECT does not choose the slice.
    with patched_env(MOP_PROJECT="other"):
        out, err, code, fixture = canonical(["mcp", "--check"], [Response(200, catalogue)])
    c.expect("#481 the ambient project does not decide the scope", code, 0)
    c.expect("#481 the scope stays explicit under a hostile environment",
             fixture.calls[-1][1], "/api/v1/capabilities?schema_version=1")
    with patched_env(MOP_PROJECT="other"):
        out, err, code, fixture = canonical(["mcp", "--check", "--project-id", PROJECT],
                                            [Response(200, dict(catalogue, scope={
                                                "project_id": PROJECT, "node_id": None,
                                                "client_id": None}))])
    c.expect("#481 an explicit --project-id is the only project scope",
             fixture.calls[-1][1],
             f"/api/v1/capabilities?schema_version=1&project_id={PROJECT}")
    # Без контекста -- отказ с именами недостающих переменных, не выдуманный
    # локальный адрес.
    out, err, code = run_command(cli.main, ["mcp", "--check"])
    c.expect("#226 --check without a context refuses", (code, out, err),
             (1, "", REFUSAL_NO_CONTEXT + "\n"))

    # #250 replacement: MOP_BRANCH не инструкция MCP, а контекст интеграционной
    # ветки, который читает lib.default_branch -- единственный потребитель.
    from mop.common import context as ctx
    with ctx.use(ctx.Context(branch="swarm")):
        c.expect("#250 the integration branch comes from the context",
                 lib.default_branch(), "origin/swarm")
    with ctx.use(ctx.Context()):
        branch = lib.default_branch()
    c.check("#250 without a branch the default origin branch applies",
            branch in ("origin/master", "origin/main") or branch.startswith("origin/"), branch)
    return True


def check_mcp_descriptions_434(c):
    """#434: модель читает канонические декларации и инструкции, а не
    внутреннее устройство канала. Легаси-описания агентов/send/tail заменены
    каноническим send-докстрингом и `_mcp.INSTRUCTIONS`; свойство прежнее:
    действие и последствие -- да, транспортный механизм -- нет."""
    text = _mcp.INSTRUCTIONS
    c.check("#434 the instructions are model-facing English", isinstance(text, str) and text)
    c.check("#434 the instructions hide legacy transport internals", not any(
        word in text for word in ("Nomad", "NATS", "tmux", "neighboring node")), text)
    c.check("#434 the instructions keep the finite surface", all(
        word in text for word in ("Authenticated management tools only", "authorizes actual resources",
                                  "private inbox unavailable")))
    path = os.path.join(cli.PACKAGE, "core", "send.py")
    doc = cli.docstring(path)
    c.check("#434 send retains the force/ownership refusal",
            "--force" in doc and "unsupported" in doc and "no trusted holder endpoint" in doc, doc)
    c.check("#434 send hides legacy transport internals", not any(
        word in doc for word in ("tmux", "skipSlashCommands", "neighboring node")), doc)
    # Единственное упоминание шины -- запрет, а не инструкция: отказ переписать
    # доставку на bus/UDS/SSH обязан остаться видимым.
    c.check("#434 the only transport mention is a refusal",
            "never bus/UDS/SSH" in doc and "fallback" in doc, doc)
    decl = declaration("send")
    c.check("#434 the declaration offers no legacy routing flags",
            not ({"--force", "--mode", "--local", "--reply-to", "--notify-when-idle"}
                 & {arg.get("flag") for arg in decl.get("args", [])}), decl.get("args"))
    c.check("#434 the tool catalogue exposes no removed surfaces",
            not ({"agents", "tail", "slash", "dispatch", "report", "master"}
                 & set(_mcp.commandlets())), sorted(_mcp.commandlets()))
    mcp_doc = open(os.path.join(cli.PACKAGE, "service", "mcp.py")).read()
    c.check("#434 --check describes an offline catalogue, not live reachability",
            "does not register an endpoint or confirm native delivery" in mcp_doc
            and "can reach" not in mcp_doc)
    return True


def check_channel_library(c):
    """Сам канал (mop/client/channel.py): потолок ожидания, путь по адресу,
    своя сессия, вердикт -> текст и тонкая обёртка доставки.

    Вердикт -- данные, а не печать; `delivered` здесь означает вердикт узла
    по его собственной квитанции, а не догадку фронтенда. Канонический
    ordinary native send этой строки не печатает вовсе (см. выше).
    """
    c.expect("MAX_WAIT", channel.MAX_WAIT, 600)
    for raw, want in ((None, 0), (0, 0), (30, 30), (9999, 600), (-5, 0)):
        c.expect(f"clamp_wait({raw})", channel.clamp_wait(raw), want)

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
            s.stack.enter_context(patched(session, sessions=lambda: [
                dict(SESS, cwd="/home/u/puppets/pu-mop-4")]))
            c.expect("my_name puppet", channel.my_name(False, "host-77"), "pu-mop-4")

    # Вердикт -> текст: доставлено, отказ агента (в том числе владельца),
    # мёртвый инбокс, неизвестная доставка.
    NOTE = "was led by olga; now yours"
    OWNED = "pu-mop-9 is led by olga (work in its clone) — force=true takes it over"
    for v, want in (
            ({"kind": "puppet", "to": PUPPET, "msg_id": "m1"}, f"{PUPPET}: delivered (msg_id=m1)"),
            ({"kind": "puppet", "to": PUPPET, "msg_id": "m1", "owner_note": NOTE},
             f"{PUPPET}: delivered (msg_id=m1); {NOTE}"),
            ({"kind": "puppet", "to": PUPPET, "error": OWNED}, f"{PUPPET}: NOT DELIVERED — {OWNED}"),
            ({"kind": "puppet", "to": PUPPET,
              "error": "pu-mop-9: the body did not answer in 20s", "timeout": True},
             f"{PUPPET}: DELIVERY UNKNOWN — pu-mop-9: the body did not answer in 20s"),
            ({"kind": "puppet", "to": PUPPET, "msg_id": "m1", "wait": 30},
             f"{PUPPET}: delivered (msg_id=m1), idle: did not wait it out in 30s"),
            ({"kind": "puppet", "to": PUPPET, "msg_id": "m1", "wait": 30,
              "idle": None, "idle_error": "idle observation failed"},
             f"{PUPPET}: delivered (msg_id=m1), idle: idle observation failed"),
            ({"kind": "puppet", "to": PUPPET, "msg_id": "m1", "notify": True},
             f"{PUPPET}: delivered (msg_id=m1); will notify when it frees up"),
            ({"kind": "local", "to": "s1", "dead": True}, "s1: inbox not listening — session is dead"),
            ({"kind": "local", "to": "s1", "msg_id": "m2", "wait": 5, "idle": {"state": "idle"}},
             "s1: delivered (msg_id=m2), idle: idle"),
            ({"kind": "master", "to": "host-1", "msg_id": "m3"}, "host-1: delivered (msg_id=m3)"),
            ({"kind": "master", "to": "host-1", "error": "gone"}, "host-1: NOT DELIVERED — gone")):
        c.expect(f"text {v}", channel.text(v), want)
    c.expect("failure of delivered", channel.failure({"kind": "puppet", "to": PUPPET, "msg_id": "m1"}), None)
    c.expect("failure of refused", channel.failure({"kind": "puppet", "to": PUPPET, "error": OWNED}),
             f"{PUPPET}: NOT DELIVERED — {OWNED}")
    c.expect("failure of indeterminate",
             channel.failure({"kind": "puppet", "to": PUPPET,
                              "error": "the body did not answer in 20s",
                              "timeout": True}),
             f"{PUPPET}: DELIVERY UNKNOWN — the body did not answer in 20s")

    # Тонкая обёртка: тело -- ops.send_to_puppet, транспорт -- bus.request
    # этого слоя (#459). Поля едут как есть, вердикт -- форма узла.
    seen = []

    def fake_ops_send(transport, node, name, message, priority="next", wait=0,
                      notify=None, **fields):
        seen.append((transport, node, name, message, priority, wait, notify, fields))
        return {"kind": "puppet", "to": name, "wait": wait, "notify": False,
                "msg_id": "id-1", "owner_note": None, "idle": None}

    def refuse_bus(*a, **k):
        raise RuntimeError("old body: bus.request called directly")
    try:
        with patched(ops, send_to_puppet=fake_ops_send), patched(bus, request=refuse_bus):
            verdict = channel.send_to_puppet("node-1", "pu-mop-9", "hello",
                                             priority="now", wait=7, owner="ermak")
    except RuntimeError as e:
        c.fail("#459 channel.send_to_puppet wraps ops.send_to_puppet", str(e))
        verdict = {}
    c.check("#459 channel.send_to_puppet wraps ops.send_to_puppet",
            seen and seen[0][1:7] == ("node-1", "pu-mop-9", "hello", "now", 7, None)
            and callable(seen[0][0]), seen)
    c.expect("#459 the wrapper returns the shared verdict", verdict.get("msg_id"), "id-1")

    # Доставка = вердикт узла: ожидание обрезается, notify не едет вместе с
    # ожиданием, неизвестный исход сохраняет маркер (#574).
    calls = []

    def transport(node, verb, **kw):
        calls.append((node, verb, kw))
        return {"msg_id": "id-1", "idle": {"state": "free (dev)"}}

    verdict = ops.send_to_puppet(transport, "node-1", "pu-x-1", "hello", wait=10_000)
    c.expect("#459 the shared operation clamps the wait",
             (calls[-1][1], calls[-1][2]["wait"], calls[-1][2]["timeout"]),
             ("send", 600, 600 + bus.TIMEOUT))
    c.expect("#459 a plain send carries no notify field", "notify" in calls[-1][2], False)
    ops.send_to_puppet(transport, "node-1", "pu-x-1", "hello", notify=True)
    c.expect("#459 notify travels when nothing waits", calls[-1][2].get("notify"), True)
    ops.send_to_puppet(transport, "node-1", "pu-x-1", "hello", wait=5, notify=True)
    c.expect("#459 notify is dropped when a wait is held", calls[-1][2].get("notify"), False)
    c.expect("#459 the verdict carries msg_id and idle",
             (verdict.get("msg_id"), verdict.get("idle", {}).get("state")),
             ("id-1", "free (dev)"))

    def indeterminate(node, verb, **kw):
        return {"error": "pu-x-1: the body did not answer in 20s", "timeout": True}
    verdict = ops.send_to_puppet(indeterminate, "node-1", "pu-x-1", "hello")
    c.check("#459 an indeterminate delivery keeps its marker",
            verdict.get("error") == "pu-x-1: the body did not answer in 20s"
            and verdict.get("timeout") is True, verdict)

    # Копий во фронтендах больше нет: потолок, тексты отказа, своя сессия.
    here = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    for rel in ("mop/cli/core/send.py", "mop/cli/service/mcp.py"):
        with open(os.path.join(here, rel)) as f:
            src = f.read()
        for copy in ("MAX_WAIT = ", "NOT DELIVERED", "inbox not listening",
                     "def master_socket", "def my_session", "def my_name",
                     "def _send_to_", "def _send_locally"):
            c.expect(f"{rel} has no copy of {copy!r}", copy in src, False)
    return True


def local_address():
    return f"{os.uname().nodename}-{os.getpid()}"


def main():
    c = Checks()

    c.check("#264 one envelope, one fallback, request_many(verb, nodes)",
            check_bus_envelope_264(c))
    c.check("#369 a stale permissions error does not explain a later timeout",
            check_stale_permissions_error_369(c))
    c.check("#371 the project roster matches the project value, not a substring",
            check_project_selection_371(c))

    try:
        check_canonical_send_793(c)
    except Exception as e:  # красный -- проваленной проверкой, а не трассой
        c.fail("#793 canonical send", f"{type(e).__name__}: {e}")
    try:
        check_role_channel_585(c)
    except Exception as e:
        c.fail("#585 role channel", f"{type(e).__name__}: {e}")
    try:
        check_role_api_585(c)
    except Exception as e:
        c.fail("#585 role api", f"{type(e).__name__}: {e}")
    try:
        check_body_binding_585(c)
    except Exception as e:
        c.fail("#585 body binding", f"{type(e).__name__}: {e}")
    try:
        check_tool_routes_585(c)
    except Exception as e:
        c.fail("#585 tool routes", f"{type(e).__name__}: {e}")
    try:
        check_private_inbox_793(c)
    except Exception as e:
        c.fail("#793 private inbox contract", f"{type(e).__name__}: {e}")
    try:
        check_mcp_check_226_481(c)
    except Exception as e:
        c.fail("#226/#481 mcp --check", f"{type(e).__name__}: {e}")
    try:
        check_mcp_descriptions_434(c)
    except Exception as e:
        c.fail("#434 mcp descriptions", f"{type(e).__name__}: {e}")
    try:
        check_channel_library(c)
    except Exception as e:
        c.fail("#148 channel library", f"{type(e).__name__}: {e}")

    # STATUS: FIXED — see #793 (эпик #623), #148, #213, #226, #371, #434, #437,
    # #459, #481, #574, #585, #662, #785
    return c.report("channel")


if __name__ == "__main__":
    sys.exit(main())
