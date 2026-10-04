#!/usr/bin/env python3
"""Завершение HTTP-intent и повторная явная команда.

HYPOTHESIS: терминал не снимает _active, а драйвер даёт воркеру ключ по
payload навсегда; даже второе отдельное сообщение читается старым результатом.
SOLUTION: живые запросы дедуплицируются, завершённые освобождают цель;
новый HTTP id становится ключом новой операции, отказ очереди атомарен.
RESULT: вторая команда исполняется, старое завершение не снимает новую.
STATUS: FIXED — see #494
"""
import os
import sys
import tempfile

import hermetic  # noqa: F401
from _lib import Checks, patched
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop.server import adminapi, opworker
from mop.cli.server import web as webcli


WHO = ("alice", "admin")


def fresh(queue=None):
    return adminapi.Api(adminapi.Operations(), queue or adminapi.Queue(), origin="https://pool.example")


def submit(api, message="hello", who=WHO):
    try:
        return api.submit("send", "pu-mop-1", {"message": message}, who)
    except adminapi.Refused as e:
        return {"error": e.code}


def main():
    c = Checks()
    for status in opworker.TERMINAL:
        api = fresh()
        first = submit(api)
        c.expect(f"#494 pending duplicate stays one intent before {status}", submit(api).get("id"), first["id"])
        api.settle(first["id"], {"status": status})
        second = submit(api, "next")
        c.check(f"#494 {status} releases the target", second.get("id") and second["id"] != first["id"], second)
        if not second.get("id"):
            continue
        api.settle(first["id"], {"status": "succeeded"})
        c.expect("#494 a late old completion cannot release the new intent", submit(api, "third"), {"error": 409})
        api.settle(second["id"], {"status": "succeeded"})
        again = submit(api)
        c.check("#494 a later explicit identical message gets a new intent", again.get("id") not in (None, first["id"], second["id"]), again)

    api = fresh()
    first = submit(api)
    api.settle(first["id"], {"status": "running"})
    c.expect("#494 nonterminal progress does not free the target", submit(api, "other"), {"error": 409})
    c.expect("#494 one admin cannot reuse another admin's pending intent", submit(api, who=("bob", "admin")), {"error": 409})

    api = fresh(adminapi.Queue(maxsize=0))
    c.expect("#494 full queue rejects", submit(api), {"error": 503})
    c.check("#494 a failed enqueue leaves no ghost result or active entry", not api._results and not api._active,
            (api._results, api._active))
    api.queue = adminapi.Queue()
    accepted = submit(api)
    job = api.queue.take()
    c.check("#494 retry after queue capacity returns really enqueues", accepted.get("id") and job
            and job["id"] == accepted["id"], (accepted, job))

    api = fresh()
    a = api.submit("add", None, {"origin": "git@example.test:g/a.git"}, WHO)
    b = api.submit("add", None, {"origin": "git@example.test:g/b.git"}, WHO)
    a2 = api.submit("add", None, {"origin": "git@example.test:g/a.git"}, WHO)
    c.expect("#494 targetless duplicate uses the same effective target", a2["id"], a["id"])
    c.check("#494 different effective targets do not replace each other", len(api._active) == 2 and a["id"] != b["id"], api._active)
    try:
        api.submit("add", None, {"origin": "git@example.test:g/a.git", "branch": "other"}, WHO)
        c.fail("#494 changed params of a pending effective target conflict", "accepted")
    except adminapi.Refused as e:
        c.expect("#494 changed params of a pending effective target conflict", e.code, 409)

    # Проверяется идентичность драйвера, не доставка: эффект намеренно
    # подменён, сам Worker и API настоящие; доставку проверяет web_send.py.
    with tempfile.TemporaryDirectory() as root:
        worker = opworker.Worker(root=root)
        effects = []
        keys = []

        class Runtime:
            def submit(self, *args, **kwargs):
                keys.append(kwargs.get("key"))
                def effect():
                    effects.append(args[3])
                    return {"outcome": "registered"}
                return worker.submit(*args, effect=effect, **kwargs)

        class Empty(Exception):
            pass

        def stop(_):
            raise Empty()

        api = fresh()
        def drain():
            with patched(webcli, ADMINAPI=api, WORKER=Runtime()), patched(webcli.time, sleep=stop):
                try:
                    webcli.drive_operations()
                except Empty:
                    pass

        first = submit(api)
        drain()
        second = submit(api)
        drain()
        c.expect("#494 driver propagates each HTTP intent id", keys, [first.get("id"), second.get("id")])
        c.expect("#494 two explicit identical messages execute twice", len(effects), 2)
    return c.report("web_intents")


if __name__ == "__main__":
    sys.exit(main())
