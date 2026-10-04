#!/usr/bin/env python3
"""CLI/MCP на общих операциях: python3 tests/cli_ops.py

HYPOTHESIS (#459): add/update/restart держали orchestration в командлетах,
send_to_puppet -- копией тела в client/channel.py и ops.py; веб (#456) зовёт
ops, а CLI -- свой путь: два пути расходятся.
SOLUTION: командлеты строят контекст (#457) и параметры на входе, зовут
mop/common/ops.py (#458) и рисуют результат; channel.send_to_puppet --
тонкая обёртка ops.send_to_puppet с bus.request транспортом. Пользовательский
контракт не меняется: те же тексты, коды и решения на тех же ответах.
STATUS: FIXED — see #459
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched  # noqa: E402
os.environ.setdefault("MOP_SERVER_LAN", "127.0.0.1")  # lib.cluster требует
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import bus  # noqa: E402
from mop.common import ops  # noqa: E402
from mop.cli.core import _common  # noqa: E402
from mop.cli import lib  # noqa: E402


class Calls:
    """Заглушка ops-операции: записывает вызов с ролью -- именем
    атрибута ops, которым его зовёт командлет (ops.add -> «add»)."""

    def __init__(self, verdict, role):
        self.seen = []
        self.verdict = verdict
        self.role = role

    def __call__(self, *args, **kwargs):
        self.seen.append((self.role, args, kwargs))
        return self.verdict


class Progress:
    def step(self, text):
        pass

    def clear(self):
        pass


def main():
    c = Checks()

    # ── add: командлет зовёт ops.add, не свой цикл ──────────────────────
    from mop.cli.core import add as cli_add
    verdict = ops.outcome(ops.REGISTERED, "pu-mop-9", marker="m1")
    calls = Calls(verdict, "add")
    with patched(_common, workspace_text=lambda o: ("text", {"source": "origin"}),
                 sent_line=lambda s: ""), \
            patched(lib, project_ready=lambda p: True, note=lambda *a: None), \
            patched(bus, login=lambda: "ermak"), \
            patched(bus, call_cluster=lambda verb, **kw: {
                "name": "pu-mop-9", "bootstrap_marker": "m1",
                "alloc": {"ClientStatus": "running",
                          "TaskStates": {"claude": {"State": "running"}}},
                "result": {"sent": "m1", "played": True, "ok": True}}), \
            patched(ops, add=calls):
        code = cli_add._add("git@h:g/mop.git", "mop", False, Progress())
    c.check("#459 add calls the shared operation",
            calls.seen and calls.seen[0][0] == "add", calls.seen)
    if calls.seen:
        args, kwargs = calls.seen[0][1], calls.seen[0][2]
        ws = args[2] if len(args) > 2 else kwargs.get("workspace")
        c.check("#459 add passes the workspace pair explicitly",
                ws == ("text", {"source": "origin"}), (args, kwargs))
        c.check("#459 add passes the branch from the context",
                "branch" in kwargs or len(args) >= 3, (args, kwargs))
    c.check("#459 add returns 0 on registered", code == 0, code)

    # Отказ общей операции -- отказ командлета с тем же текстом.
    calls = Calls(ops.outcome(ops.REFUSED, "pu-mop-9", error="no space on node"), "add")
    said = []
    with patched(_common, workspace_text=lambda o: ("", None),
                 sent_line=lambda s: ""), \
            patched(lib, project_ready=lambda p: True, note=lambda *a: None,
                    fail=said.append), \
            patched(bus, login=lambda: "ermak"), \
            patched(bus, call_cluster=lambda verb, **kw: {
                "name": "pu-mop-9", "bootstrap_marker": "m2",
                "alloc": {"ClientStatus": "running",
                          "TaskStates": {"claude": {"State": "running"}}},
                "result": {"sent": "m2", "played": True, "ok": True}}), \
            patched(ops, add=calls):
        code = cli_add._add("git@h:g/mop.git", "mop", False, Progress())
    c.check("#459 a refused shared operation fails the commandlet",
            code != 0 and said and "no space" in said[0], (code, said))

    # ── update: командлет зовёт ops.update ──────────────────────────────
    from mop.cli.core import update as cli_update
    calls = Calls(ops.outcome(ops.ACCEPTED, "pu-mop-9"), "update")
    with patched(_common, workspace_text=lambda o: ("t", None),
                 sent_line=lambda s: ""), \
            patched(lib, note=lambda *a: None), \
            patched(bus, call_cluster=lambda verb, **kw: {"meta": {"origin": "o"},
                                                          "bootstrap_marker": None,
                                                          "replaced": None}), \
            patched(bus, login=lambda: "ermak"), \
            patched(ops, update=calls):
        code = cli_update.main(["pu-mop-9"])
    c.check("#459 update calls the shared operation",
            calls.seen and calls.seen[0][0] == "update", calls.seen)

    # ── restart: командлет зовёт ops.restart ────────────────────────────
    from mop.cli.core import restart as cli_restart
    calls = Calls(ops.outcome(ops.ACCEPTED, "pu-mop-9"), "restart")
    with patched(lib, note=lambda *a: None), \
            patched(cli_restart.puppets, running_alloc=lambda n: True), \
            patched(bus, call_cluster=lambda verb, **kw: {"ok": True}), \
            patched(bus, login=lambda: "ermak"), \
            patched(ops, restart=calls):
        code = cli_restart.main(["pu-mop-9"])
    c.check("#459 restart calls the shared operation",
            calls.seen and calls.seen[0][0] == "restart", calls.seen)

    # ── channel.send_to_puppet -- тонкая обёртка ops.send_to_puppet ────
    from mop.client import channel
    seen = []

    def fake_ops_send(transport, node, name, message, priority="next", wait=0,
                      notify=None, **fields):
        seen.append((transport, node, name, message, priority, wait, notify, fields))
        return {"kind": "puppet", "to": name, "wait": wait, "notify": False,
                "msg_id": "id-1", "owner_note": None, "idle": None}

    def refuse_bus(*a, **k):
        raise RuntimeError("old body: bus.request called directly")
    try:
        with patched(ops, send_to_puppet=fake_ops_send), \
                patched(bus, request=refuse_bus):
            v = channel.send_to_puppet("node-1", "pu-mop-9", "hello",
                                       priority="now", wait=7, owner="ermak")
    except RuntimeError as e:
        c.fail("#459 channel.send_to_puppet wraps ops.send_to_puppet", str(e))
        v = {}
    # Транспорт подменён отказом шины: обёртка обязана была ЗВАТЬ ops
    # (иначе старое тело ударилось бы в bus.request само), и все поля
    # доехали до ops как есть.
    c.check("#459 channel.send_to_puppet wraps ops.send_to_puppet",
            seen and seen[0][1:7] == ("node-1", "pu-mop-9", "hello", "now", 7, None)
            and callable(seen[0][0]), seen)
    c.check("#459 the wrapper returns the shared verdict",
            v.get("msg_id") == "id-1", v)

    # В теле channel больше нет копии: строк bus.request|send в нём не осталось
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.realpath(__file__))), "mop/client/channel.py")).read()
    body = src[src.index("def send_to_puppet"):] if "def send_to_puppet" in src else ""
    body = body[:body.index("\ndef ")] if "\ndef " in body else body
    code = "\n".join(l for l in body.splitlines()
                     if not l.lstrip().startswith(("#", chr(34))))
    c.check("#459 the old copy of the delivery body is gone from channel",
            "ops.send_to_puppet" in code and "bus.request" in code
            and "clamp_wait" not in code and "msg_id" not in code, code[:200])

    return c.report("cli_ops")


if __name__ == "__main__":
    sys.exit(main())
