#!/usr/bin/env python3
"""Политика очистки из кластерного сервиса: python3 tests/gc.py."""
import os
import sys

import hermetic  # noqa: F401,E402
from _lib import Checks, patched, patched_env, run_command  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.cli.server import gc, _maintenance  # noqa: E402
from mop.common import state  # noqa: E402
from mop.server import cluster  # noqa: E402


# HYPOTHESIS: клиент берёт пороги из своей .env; два оператора видят разные
# планы на одном пуле. Недоступность сервиса не должна запускать очистку.
# SOLUTION: политика читается одним операторским RPC с сервера до ростера;
# --dry использует те же пороги, но ничего не пересоздаёт.
# RESULT: пороги сервера определяют dry-run; при отказе RPC клиент не действует.
# STATUS: FIXED — see #398
def check_server_policy_398(c):
    from mop.common import config
    with patched(config, get=lambda name, default=None: {"MOP_GC_FREE_MIN_GB": "29",
                                                         "MOP_GC_MAX_PER_RUN": "2"}.get(name, "")):
        c.expect("#398 policy is server-owned", cluster.answer("admin", {"verb": "gc_policy"}),
                 {"ok": True, "free_min_gb": 29, "max_per_run": 2})
    c.check("#398 project masters cannot set or read global policy",
            cluster.refusal("mop", "gc_policy") is not None)

    row = state.PuppetRow("pu-mop-1", "node-a", "running", "free", "free",
                          None, "git@h:g/mop.git", 1000)
    asked, recycled = [], []
    with patched_env(MOP_SERVER_LAN="server.test", MOP_GC_FREE_MIN_GB="0",
                     MOP_GC_MAX_PER_RUN="0"), \
            patched(gc.bus, call_cluster=lambda verb: asked.append(verb) or
                    {"ok": True, "free_min_gb": 25, "max_per_run": 1},
                    request_many=lambda *a: {"node-a": {"free_gb": 20}}), \
            patched(gc.puppets, puppet_rows=lambda: [row],
                    recycle=lambda name: recycled.append(name)):
        with patched(_maintenance, require=lambda: None):
            out, _, code = run_command(gc.main, ["--dry"])
    c.expect("#398 client asks server for policy", asked, ["gc_policy"])
    c.check("#398 dry run uses server threshold", "20 GB free < 25" in out, out)
    c.expect("#398 dry run does not recycle", recycled, [])
    c.expect("#398 dry run succeeds", code, 0)

    with patched_env(MOP_SERVER_LAN="server.test"), \
            patched(gc.bus, call_cluster=lambda *a: (_ for _ in ()).throw(RuntimeError("offline"))), \
            patched(gc.puppets, puppet_rows=lambda: c.fail("#398 no roster on policy failure")):
        try:
            with patched(_maintenance, require=lambda: None):
                gc.main(["--dry"])
            c.fail("#398 server outage must refuse, not use local defaults")
        except RuntimeError:
            pass


def main():
    c = Checks()
    check_server_policy_398(c)
    return c.report("gc")


if __name__ == "__main__":
    sys.exit(main())
