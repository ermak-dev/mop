#!/usr/bin/env python3
"""Приватный HTTP/CLI чат: права, Origin и отсутствие операции-очереди (#600).

HYPOTHESIS: /api/puppet ещё не имеет истории и настоящего терминального ввода.
SOLUTION: отдельные приватные маршруты, reauth, cookie binding и named CLI/MCP;
клавиатура не проходит Operations, неопределённая доставка не повторяется.
RESULT: RED маршрутов и CLI diagnostics; GREEN вместе с прежним web контрактом.
STATUS: FIXED — see #600
"""
import base64
import io
import json
import os
import sys

import hermetic  # noqa: F401
from _lib import Checks, patched
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop import cli
from mop.cli.server import web as app
from mop.common import bus
from mop.common.state import PuppetRow
from mop.server.terminal import TerminalBridge


NAME = "pu-actual-1"
ORIGIN = "https://pool.example"
ROWS = [PuppetRow(name=NAME, node="node-a", alloc_status="running", state="busy",
                  kind="busy", owner="alice", origin="git@example.test:g/actual.git", disk_kb=None)]


def main():
    c = Checks()
    calls, audit = [], []
    actor = [("alice", "admin")]
    read_hook = [None]
    history = {"epoch": "epoch-a", "session_id": "s1", "cursor": "cursor-a", "records": [
        {"type": "assistant", "message": {"role": "assistant", "content": "private conversation"}}],
        "reset": False, "has_more": False, "partial": False}
    counter = [0]

    def request(node, verb, **fields):
        calls.append((node, verb, fields))
        if verb == "history":
            if read_hook[0]:
                read_hook[0]()
            return history
        if verb == "state":
            return {"clone": {"cur": "master", "def": "master", "dirty": 1, "ahead": 0,
                              "owner": {"user": "alice", "at": 1}}}
        if verb == "term_open":
            counter[0] += 1
            return {"tid": f"terminal-{counter[0]}", "epoch": "epoch-a", "cursor": 0,
                    "mode": fields["mode"], "cols": fields["cols"], "rows": fields["rows"]}
        if verb == "term_read":
            return {"data": "G1tIaGVsbG8=", "cursor": 8, "reset": False,
                    "closed": False, "exit": None, "epoch": "epoch-a"}
        if verb == "term_write":
            return {"seq": fields["seq"], "written": len(base64.b64decode(fields["data"])),
                    "outcome": "written"}
        return {"ok": True}

    bridge = TerminalBridge(request, lambda name: {"node": "node-a", "project": "actual"},
                            audit=audit.append)

    def http(method, path, payload=None, *, origin=ORIGIN, cookie="browser-a", declared=None):
        body = b"" if payload is None else json.dumps(payload).encode()
        h = app.Handler.__new__(app.Handler)
        h.command, h.path = method, path
        h.headers = {"Cookie": f"{app.websession.COOKIE}={cookie}", "Content-Type": "application/json",
                     "Content-Length": str(len(body) if declared is None else declared), "Origin": origin}
        h.rfile, h.wfile, h.sent = io.BytesIO(body), io.BytesIO(), []
        h.client_address = ("127.0.0.1", 0)
        h.send_response = lambda code, message=None: h.sent.append(("status", code))
        h.send_header = lambda key, value: h.sent.append((key, value))
        h.end_headers = lambda: None
        setattr(h, "_who", lambda: actor[0])
        h._origin_ok = lambda: h.headers.get("Origin") == ORIGIN
        getattr(h, f"do_{method}")()
        data = h.wfile.getvalue()
        try:
            data = json.loads(data)
        except ValueError:
            pass
        return h.sent[0][1], data, h.sent

    collector = type("Collector", (), {"rows": ROWS})()
    has_bridge = hasattr(app, "TERMINALS")
    c.check("#600 HTTP frontend has a private terminal bridge", has_bridge)
    sessions = type("Sessions", (), {"get": lambda self, token: actor[0] if token in ("browser-a", "browser-b") and actor[0][0] else None,
                                      "close": lambda self, token: True})()
    patches = {"COLLECTOR": collector, "SESSIONS": sessions}
    if has_bridge:
        patches["TERMINALS"] = bridge
    with patched(app, **patches), patched(bus, request=request):
        for who, code in (((None, None), 401), (("alice", "user"), 403)):
            actor[0] = who
            before = len(calls)
            for method, path, payload in (("GET", f"/api/puppet/{NAME}/history", None),
                                          ("POST", f"/api/puppet/{NAME}/term", {"mode": "rw", "cols": 100, "rows": 30})):
                status, data, _ = http(method, path, payload)
                c.expect("#600 private routes require current admin", status, code)
            c.expect("#600 denied viewer never reaches node", len(calls), before)
        actor[0] = ("alice", "admin")
        status, data, headers = http("GET", f"/api/puppet/{NAME}/history")
        c.expect("#600 structured history has its own private route", (status, data), (200, history))
        c.check("#600 private history is never cached", ("Cache-Control", "no-store") in headers, headers)
        if calls:
            c.check("#600 history rides actual target project",
                    calls[-1][1] == "history" and calls[-1][2].get("project") == "actual", calls)
        read_hook[0] = lambda: actor.__setitem__(0, ("alice", "user"))
        status, data, _ = http("GET", f"/api/puppet/{NAME}/history")
        c.check("#600 revoked viewer receives no waited-for conversation",
                status == 403 and "private conversation" not in str(data), (status, data))
        read_hook[0] = None
        actor[0] = ("alice", "admin")
        before = len(calls)
        for path in (f"/api/puppet/{NAME}/history?path=/etc/passwd",
                     f"/api/puppet/{NAME}/history?cursor=a&cursor=b"):
            c.expect("#600 history does not accept arbitrary paths or shadowed cursor", http("GET", path)[0], 400)
        c.expect("#600 invalid history query has no node effect", len(calls), before)
        c.expect("#600 unknown target refuses before node",
                 http("GET", "/api/puppet/pu-other-2/history")[0], 404)
        for payload, origin, declared, code in (
                ({"mode": "rw", "cols": 100, "rows": 30}, "https://evil.example", None, 403),
                ({"mode": "rw", "cols": 100, "rows": 30, "owner": "bob"}, ORIGIN, None, 400),
                ({"mode": "rw", "cols": 100, "rows": 30}, ORIGIN, 99999, 413)):
            c.expect("#600 keyboard request validates Origin, shape and body limit",
                     http("POST", f"/api/puppet/{NAME}/term", payload, origin=origin, declared=declared)[0], code)
        status, opened, _ = http("POST", f"/api/puppet/{NAME}/term", {"mode": "rw", "cols": 100, "rows": 30})
        c.expect("#600 taking control is synchronous private attach", status, 200)
        if status == 200:
            tid = opened["tid"]
            status, got, _ = http("POST", f"/api/puppet/{NAME}/term/{tid}/input", {"seq": 1, "data": "Gw=="})
            c.expect("#600 Stop bytes travel outside admin operation queue", (status, got.get("outcome")), (200, "written"))
            c.check("#600 Stop does not invoke job lifecycle or Escape RPC",
                    not any(v in ("stop", "job_stop", "restart", "type") for _, v, _ in calls), calls)
            c.expect("#600 another browser cannot reuse terminal control",
                     http("POST", f"/api/puppet/{NAME}/term/{tid}/input", {"seq": 2, "data": "YWJj"}, cookie="browser-b")[0], 403)
            mixed = f"invalid; {app.websession.COOKIE}=browser-a"
            c.expect("#600 handle binding uses the accepted cookie, not the first token",
                     http("POST", f"/api/puppet/{NAME}/term/{tid}/input", {"seq": 2, "data": "YWJj"}, cookie=mixed)[0], 200)
            status, got, _ = http("GET", f"/api/puppet/{NAME}/term/{tid}/read?cursor=0&wait=15")
            c.expect("#600 PTY reads preserve ANSI bytes", (status, got.get("data")), (200, "G1tIaGVsbG8="))
            c.expect("#600 explicit close detaches only client",
                     http("POST", f"/api/puppet/{NAME}/term/{tid}/close", {})[0], 200)
        c.check("#600 terminal audit contains neither tokens nor key bytes",
                all("tid" not in entry and "data" not in entry for entry in audit), audit)

    catalog = cli.catalog(cli.scan())
    c.check("#600 named session commands exist", "session" in catalog, catalog.keys())
    c.check("#600 session commands use client credentials, not installation env",
            cli.client_command("mop.cli.session.history"))
    if "session" in catalog:
        from contextlib import redirect_stdout
        from mop.cli import lib
        with patched(lib, cluster=lambda fn: fn):
            from mop.cli.session import history as history_cli, terminal as terminal_cli
        ctx = lambda name: ({"node": "node-a", "project": "actual"}, "alice")
        out = io.StringIO()
        with patched(terminal_cli, context=ctx), patched(bus, request=request), redirect_stdout(out):
            code = terminal_cli.main([NAME, "interrupt", "terminal-1", "--seq", "1"])
        c.expect("#600 CLI Stop is real terminal input, not job lifecycle", code, 0)
        c.check("#600 CLI owner is the verified bus principal",
                calls[-1][1] == "term_write" and calls[-1][2]["owner"] == "alice"
                and calls[-1][2]["data"] == "Gw==", calls[-1])

        def lost(*args, **kwargs):
            raise RuntimeError("PRIVATE-RAW-ERROR after possible delivery")

        out = io.StringIO()
        try:
            with patched(terminal_cli, context=ctx), patched(bus, request=lost), redirect_stdout(out):
                code = terminal_cli.main([NAME, "input", "terminal-1", "--seq", "2", "--data", "YWJj"])
        except RuntimeError:
            c.check("#600 CLI reports uncertain input as unknown", False, "raw transport exception")
        else:
            got = json.loads(out.getvalue())
            c.check("#600 CLI reports uncertain input as unknown",
                    code == 2 and got["outcome"] == "unknown" and "PRIVATE" not in str(got), got)

        def timed_out(*args, **kwargs):
            raise bus.BusError("node answer timed out")

        out = io.StringIO()
        try:
            with patched(terminal_cli, context=ctx), patched(bus, request=timed_out), redirect_stdout(out):
                code = terminal_cli.main([NAME, "input", "terminal-1", "--seq", "3", "--data", "YWJj"])
        except bus.BusError:
            c.check("#600 a safe library timeout keeps its reason", False, "no structured verdict")
        else:
            got = json.loads(out.getvalue())
            c.check("#600 a safe library timeout keeps its reason",
                    code == 2 and got["outcome"] == "unknown" and got["error"] == "node answer timed out", got)
        from mop.cli.session import _common as session_cli
        with patched(session_cli.puppets, _cluster=lambda *args, **kwargs: {}), patched(bus, login=lambda: "alice"), patched(lib, guard=lambda name: None):
            try:
                session_cli.context(NAME)
            except bus.Refused as e:
                c.expect("#600 a context refusal reaches CLI without traceback", str(e), "cannot determine the target project")
            else:
                c.check("#600 a context refusal reaches CLI without traceback", False, "missing metadata passed")
    return c.report("chat_api")


if __name__ == "__main__":
    sys.exit(main())
