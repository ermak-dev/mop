#!/usr/bin/env python3
"""CLI/MCP на общих операциях: python3 tests/cli_ops.py

HYPOTHESIS (#459): add/update/restart держали orchestration в командлетах,
send_to_puppet -- копией тела в client/channel.py и ops.py; веб (#456) зовёт
ops, а CLI -- свой путь: два пути расходятся.
SOLUTION (#793): свойства #459 переведены на канонический путь #642.
add/restart/update -- тонкие командлеты над одним transient HTTPS consumer
`_control.invoke` (та же поверхность, что у tests/control_consumers.py):
командлет не собирает ни политику, ни ветку, ни bus/ops-оркестрацию.
channel.send_to_puppet по-прежнему тонкая обёртка ops.send_to_puppet с
bus.request транспортом. Update admission в этом релизе не связан -- явный
unsupported с точным отказом (см. отчёт #793). Пользовательский контракт не
меняется: те же тексты, коды и решения на тех же ответах.
STATUS: FIXED — see #793
"""
import contextlib
import json
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, patched_env, run_command  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from _control_fixture import (DEFINITION, EPOCH, Fixture, KEY, NODE,  # noqa: E402
                              PROJECT, Response, TOKEN, create_operation)
from mop import cli  # noqa: E402
from mop.cli import _control  # noqa: E402
from mop.cli.core import _common  # noqa: E402
from mop.cli.core import add as cli_add  # noqa: E402
from mop.client import control as api  # noqa: E402
from mop.common import bus, context, ops, puppets  # noqa: E402

API = "https://controller.example"
CREATE = [PROJECT, "--node-id", NODE, "--definition-id", DEFINITION,
          "--epoch", EPOCH, "--request-id", KEY, "--not-after",
          "2026-10-16T10:05:00Z"]


@contextlib.contextmanager
def no_legacy(reads):
    """Legacy-оркестрация #459 под отказом: канонический путь её не зовёт."""
    def refuse(*a, **k):
        raise AssertionError("legacy orchestration reached on the canonical path")
    with patched(ops, add=refuse, update=refuse, restart=refuse), \
            patched(puppets, recycle=refuse), \
            patched(context, here=refuse), \
            patched(_common, workspace_text=lambda o: reads.append(o) or
                    ("text", {"source": "origin"})):
        yield


def probe(argv, *responses, words="add"):
    """Командлет на каноническом шве -> (fixture, out, err, code, reads)."""
    fx = Fixture(*responses)
    reads = []
    with patched(api, _DeadlineHTTPSConnection=fx.factory), \
            _control.use(api.ServerContext(API, TOKEN)), no_legacy(reads):
        out, err, code = run_command(cli.main, [words, *argv])
    return fx, out, err, code, reads


def body_of(fx):
    return json.loads(fx.calls[-1][2])


def main():
    c = Checks()

    # ── add: командлет зовёт общий HTTPS handler, не свой цикл ──────────
    # HYPOTHESIS: add собирал контекст и оркестрацию сам.
    # SOLUTION: тело командлета -- один вызов _control.invoke('add', argv);
    # аргументы едут без изменений, как у control_consumers.
    routed = []
    def handler(surface, argv):
        routed.append((surface, list(argv)))
        return 0
    with patched(_control, invoke=handler), no_legacy([]):
        code = cli_add.main(CREATE)
    c.check("#459 add calls the shared operation",
            routed == [("add", CREATE)], routed)
    c.check("#459 add returns the shared operation's exit code", code == 0, code)

    # Провод: DRF create-puppet payload -- явные node/definition и ключ.
    fx, out, err, code, reads = probe(CREATE, Response(202, create_operation("succeeded")))
    add = body_of(fx)
    c.check("#459 add passes the workspace pair explicitly",
            add["params"] == {"node_id": NODE, "definition_id": DEFINITION}
            and "workspace" not in add and reads == [], (add, reads))
    c.check("#459 add passes the branch from the context",
            fx.calls[0][1] == "/api/v1/projects/" + PROJECT + "/commands/create-puppet"
            and "branch" not in add and "origin" not in add,
            (fx.calls[0][1], sorted(add)))
    c.check("#459 add returns 0 on registered", code == 0, (code, err))

    # Отказ общей операции -- отказ командлета с тем же текстом.
    fx, out, err, code, reads = probe(
        CREATE, Response(403, {"schema_version": 1, "error": {
            "code": "permission_denied", "message": "no space on node",
            "retryable": False}}))
    c.check("#459 a refused shared operation fails the commandlet",
            code == 1 and out == "" and "no space on node" in err
            and "permission_denied" in err, (code, out, err))

    # ── update: admission не связан -- явный отказ, не общий цикл ───────
    # HYPOTHESIS: update читал политику и звал ops.update.
    # SOLUTION (#793): один канонический отказ до сети и до чтения политики.
    fx, out, err, code, reads = probe(["pu-mop-9"], words="update")
    c.check("#459 update calls the shared operation",
            code == 2 and out == "" and fx.calls == [] and reads == []
            and err == "unsupported: update admission is not bound in this release\n",
            (code, out, err, fx.calls, reads))
    with patched_env(MOP_SOUL_DIR="/role-bundle", MOP_PUPPET="pu-alpha-1"):
        fx, out, err, code, reads = probe(["pu-mop-9"], words="update")
    c.check("#459 the granted update takes the same canonical refusal",
            code == 2 and fx.calls == [] and reads == []
            and err == "unsupported: update admission is not bound in this release\n",
            (code, err, fx.calls, reads))

    # ── restart: тот же HTTPS handler, не прежний клиентский workflow ───
    # Полный wire/unknown/CAS проверяется отдельно в control_consumers.
    from mop.cli.core import restart as cli_restart
    args = ['9db85243-4ae1-4511-8acc-bf6c8140cf2c']
    routed = []
    def https(surface, argv):
        routed.append((surface, argv))
        return 3
    def no_old_restart(*a, **k):
        raise AssertionError('restart reached legacy client orchestration')
    with patched(_control, invoke=https), patched(ops, restart=no_old_restart):
        code = cli_restart.main(args)
    c.expect('#642 restart passes unchanged args to the HTTPS handler', routed, [('restart', args)])
    c.expect('#642 restart preserves the outstanding operation exit', code, 3)

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
