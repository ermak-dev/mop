#!/usr/bin/env python3
"""Приватный мост терминала поверх узлового PTY (#599).

HYPOTHESIS: сервер не держит приватные терминальные подключения и отдельного
writer; административная очередь не подходит для клавиатуры и Stop.
SOLUTION: bounded registry с principal/browser binding и прямыми PTY verbs;
reauth после чтения, отказ чужому writer, неизвестный ввод без автоповтора.
RESULT: RED на отсутствующем мосте и трёх потерянных свойствах; GREEN матрицы.
STATUS: FIXED — see #599
"""
import base64
import importlib.util
import os
import sys
import threading

import hermetic  # noqa: F401
from _lib import Checks
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop.common import bus


def main():
    c = Checks()
    available = importlib.util.find_spec("mop.server.terminal") is not None
    c.check("#599 a private terminal bridge exists", available)
    if not available:
        return c.report("terminal_bridge")
    from mop.server.terminal import BridgeError, TerminalBridge

    target = "pu-actual-1"
    clean = {"clone": {"cur": "master", "def": "master", "dirty": 0,
                       "ahead": 0, "owner": None}}

    class Rig:
        def __init__(self, facts=clean, maximum=8):
            self.now = 100.0
            self.actor = ("alice", "admin", "browser-a")
            self.facts = facts
            self.calls, self.audit = [], []
            self.read_hook = None
            self.answer = None
            self.counter = 0
            self.authorizations = 0
            self.bridge = TerminalBridge(self.call, self.resolve, audit=self.audit.append,
                                         clock=lambda: self.now, maximum=maximum)

        def auth(self):
            self.authorizations += 1
            return self.actor

        def resolve(self, name):
            return {"node": "node-a", "project": "actual"}

        def call(self, node, verb, **fields):
            self.calls.append((node, verb, fields))
            if verb == "state":
                return self.facts
            if self.answer is not None:
                if isinstance(self.answer, Exception):
                    raise self.answer
                return self.answer
            if verb == "term_open":
                self.counter += 1
                return {"tid": f"terminal-{self.counter}", "epoch": "epoch-a",
                        "cursor": 0, "mode": fields["mode"],
                        "cols": fields["cols"], "rows": fields["rows"]}
            if verb == "term_read":
                if self.read_hook:
                    self.read_hook()
                return {"data": base64.b64encode(b"\x1b[Hhello").decode(), "cursor": 8,
                        "reset": False, "closed": False, "exit": None, "epoch": "epoch-a"}
            if verb == "term_write":
                return {"written": 3, "seq": fields["seq"], "outcome": "written"}
            return {"ok": True}

        def open(self, mode="rw"):
            return self.bridge.open(target, mode, 100, 30, self.auth)

    def refused(label, fn, code):
        try:
            fn()
        except BridgeError as e:
            c.expect(label, e.code, code)
        else:
            c.check(label, False, "no refusal")

    for actor, code in (((None, None, "guest"), 401),
                        (("alice", "user", "browser-a"), 403)):
        r = Rig()
        r.actor = actor
        refused("#599 open requires current admin", r.open, code)
        c.check("#599 unauthorised open never reaches node", not r.calls, r.calls)

    for facts in (None, {}, {"error": "agent silent"},
                  {"clone": dict(clean["clone"], dirty=1)},
                  {"clone": dict(clean["clone"], dirty=1, owner={"user": "bob", "at": 1})},
                  {"clone": dict(clean["clone"], owner={"user": "alice", "at": 1},
                                 dirty=None, ahead=None)}):
        r = Rig(facts)
        refused("#599 unsafe writer is refused before attach", r.open, 409)
        c.check("#599 safety refusal opens no PTY",
                all(v == "state" for _, v, _ in r.calls), r.calls)
    r = Rig({"clone": dict(clean["clone"], dirty=2, ahead=3,
                           owner={"user": "alice", "at": 1})})
    opened = r.open()
    tid = opened["tid"]
    c.expect("#599 owned work can take terminal control", opened["mode"], "rw")
    c.check("#599 node attachment is private admin subject",
            r.calls[-1][2]["project"] == bus.ADMIN
            and r.calls[-1][2]["owner"] == "alice", r.calls)
    c.check("#599 control audit contains no terminal token or contents",
            bool(r.audit) and all("tid" not in a and "data" not in a for a in r.audit), r.audit)
    before = len(r.calls)
    refused("#599 a second writer cannot race the first", r.open, 409)
    c.expect("#599 second writer never starts another node attach", len(r.calls), before)
    observer = r.open("ro")
    c.expect("#599 readers coexist with the writer", observer["mode"], "ro")
    refused("#599 reader cannot write",
            lambda: r.bridge.write(target, observer["tid"], 1, "YWJj", r.auth), 403)
    refused("#599 reader cannot resize",
            lambda: r.bridge.resize(target, observer["tid"], 120, 40, r.auth), 403)
    for actor in (("bob", "admin", "browser-b"), ("alice", "admin", "browser-b")):
        r.actor = actor
        refused("#599 handle is bound to principal and browser",
                lambda: r.bridge.read(target, tid, 0, r.auth), 403)
    r.actor = ("alice", "admin", "browser-a")
    refused("#599 a token is bound to its puppet",
            lambda: r.bridge.read("pu-actual-2", tid, 0, r.auth), 404)
    r.authorizations = 0
    read = r.bridge.read(target, tid, 0, r.auth, wait=15)
    c.expect("#599 raw terminal bytes are preserved", read["data"], "G1tIaGVsbG8=")
    c.expect("#599 long-poll reauthenticates after node reply", r.authorizations, 2)
    r.read_hook = lambda: setattr(r, "actor", ("alice", "user", "browser-a"))
    refused("#599 revoked access never returns waited-for bytes",
            lambda: r.bridge.read(target, tid, 8, r.auth), 403)
    c.check("#599 revoked reader releases its node client",
            any(v == "term_close" and f.get("tid") == tid for _, v, f in r.calls), r.calls)

    r = Rig()
    tid = r.open()["tid"]

    def provider_failed():
        raise BridgeError(503, "login provider unavailable")

    refused("#599 a structured authentication failure keeps its code and reason",
            lambda: r.bridge.read(target, tid, 0, provider_failed), 503)
    r = Rig()
    tid = r.open()["tid"]
    for seq, data in ((0, "YWJj"), (True, "YWJj"), (1, "not-base64"),
                      (1, base64.b64encode(b"x" * 4097).decode())):
        refused("#599 input schema fails before effect",
                lambda seq=seq, data=data: r.bridge.write(target, tid, seq, data, r.auth), 400)
    c.check("#599 malformed input never reaches PTY",
            not any(v == "term_write" for _, v, _ in r.calls), r.calls)
    written = r.bridge.write(target, tid, 1, "YWJj", r.auth)
    c.expect("#599 writes use independent direct transport", written["outcome"], "written")
    r.answer = RuntimeError("secret raw error")
    got = r.bridge.write(target, tid, 2, "YWJj", r.auth)
    c.expect("#599 loss after writing is unknown", got["outcome"], "unknown")
    c.check("#599 write is not replayed by the bridge",
            sum(v == "term_write" for _, v, _ in r.calls) == 2, r.calls)
    c.check("#599 raw exception text is not returned", "secret" not in str(got), got)

    r = Rig()
    tid = r.open()["tid"]
    r.answer = {"error": "the body did not answer", "timeout": True}
    got = r.bridge.write(target, tid, 1, "YWJj", r.auth)
    c.expect("#599 an indeterminate node verdict remains unknown", got["outcome"], "unknown")
    r.answer = {"written": 3, "seq": 2, "outcome": "written", "secret_extra": "hidden"}
    got = r.bridge.write(target, tid, 2, "YWJj", r.auth)
    c.check("#599 only declared input result fields leave the bridge", "secret_extra" not in got, got)
    r.answer = {"written": 1, "seq": 3, "outcome": "written"}
    c.expect("#599 a partial byte receipt cannot claim full input written",
             r.bridge.write(target, tid, 3, "YWJj", r.auth)["outcome"], "unknown")
    r.answer = {"data": "YWJj", "cursor": 1, "reset": False,
                "closed": False, "exit": None, "epoch": "epoch-a"}
    refused("#599 a cursor cannot move backwards without reset",
            lambda: r.bridge.read(target, tid, 8, r.auth), 502)

    r = Rig()
    tid = r.open()["tid"]
    r.read_hook = lambda: None
    r.answer = {"data": "YWJj", "cursor": 3, "reset": False,
                "closed": False, "exit": None, "epoch": "epoch-other"}
    refused("#599 stale conversation cannot use old handle",
            lambda: r.bridge.read(target, tid, 0, r.auth), 409)
    r = Rig()
    r.open()
    r.now += 61
    c.expect("#599 expired writer permits a new explicit take", r.open()["mode"], "rw")
    c.check("#599 expiration closes only attach clients",
            any(v == "term_close" for _, v, _ in r.calls)
            and not any(v in ("kill", "stop", "restart", "delete") for _, v, _ in r.calls), r.calls)
    r = Rig()
    r.open()
    r.now += 31
    try:
        new = r.open()
    except BridgeError as e:
        c.check("#599 writer expiry agrees with node's 30 seconds", False, e.error)
    else:
        c.expect("#599 writer expiry agrees with node's 30 seconds", new["mode"], "rw")
    r = Rig()
    tid = r.open()["tid"]
    for seq in (1, 2, 3):
        r.now += 25
        try:
            got = r.bridge.write(target, tid, seq, "YWJj", r.auth)
        except BridgeError as e:
            c.check("#599 successful keyboard activity renews control", False, e.error)
        else:
            c.expect("#599 successful keyboard activity renews control", got["outcome"], "written")
    r = Rig(maximum=1)
    r.open("ro")
    refused("#599 server has a hard attachment cap", lambda: r.open("ro"), 429)
    r = Rig()
    tid = r.open()["tid"]
    read_entered, read_release = threading.Event(), threading.Event()
    read_results = []

    def blocked_read(node, verb, **fields):
        if verb == "term_read":
            read_entered.set()
            read_release.wait(3)
        return r.call(node, verb, **fields)

    r.bridge.transport = blocked_read
    thread = threading.Thread(target=lambda: read_results.append(r.bridge.read(target, tid, 0, r.auth)))
    thread.start()
    try:
        c.check("#599 one long poll reserves its private reader", read_entered.wait(2))
        c.expect("#599 keyboard control does not await a long poll",
                 r.bridge.write(target, tid, 1, "YWJj", r.auth)["outcome"], "written")
        refused("#599 duplicate long polls are bounded per attachment",
                lambda: r.bridge.read(target, tid, 0, r.auth), 409)
    finally:
        read_release.set()
        thread.join(3)
    c.check("#599 the first reader completes without blocking keyboard control",
            len(read_results) == 1, read_results)

    r = Rig()
    entered, release = threading.Event(), threading.Event()
    results = []

    def delayed(node, verb, **fields):
        if verb == "term_open":
            entered.set()
            release.wait(3)
        return r.call(node, verb, **fields)

    r.bridge.transport = delayed
    thread = threading.Thread(target=lambda: results.append(r.open()))
    thread.start()
    try:
        c.check("#599 first writer reserves control before awaiting attach", entered.wait(2))
        refused("#599 concurrent writer is refused while first attach waits", r.open, 409)
    finally:
        release.set()
        thread.join(3)
    c.check("#599 only one writer survives concurrent opens",
            len(results) == 1 and sum(v == "term_open" for _, v, _ in r.calls) == 1, r.calls)
    r = Rig()
    tid = r.open()["tid"]
    r.bridge.revoke("browser-a")
    refused("#599 logout removes private handles",
            lambda: r.bridge.read(target, tid, 0, r.auth), 404)
    c.check("#599 logout detaches without killing session",
            [v for _, v, _ in r.calls][-1] == "term_close", r.calls)
    return c.report("terminal_bridge")


if __name__ == "__main__":
    sys.exit(main())
