#!/usr/bin/env python3
"""Публичная причина завершения не раскрывает частные ошибки воркера.

HYPOTHESIS: error записан в исполнении, но result выбрасывает его целиком.
SOLUTION: публиковать только закрытые фразы, а не сырые исключения,
payload сообщения, пути или ключи исполнителя (#495); с #499 вердикт
едет тем же settle-путём -- через завершение потока операции, без
очереди и журнала.
"""
import json
import os
import sys

import hermetic  # noqa: F401
from _lib import Checks, patched
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop.server import adminapi


def effect(status, error):
    """Эффект-подмена: сразу терминальный вердикт #476."""
    if error is None:
        return {"outcome": "registered"}
    kind = {"refused": "refused", "failed": "failed",
            "partial": "partial", "unknown": "unknown"}[status]
    return {"outcome": kind, "error": error}


# HYPOTHESIS: конкретные закрытые workflow-причины повторно проходят
# result_error и исчезают, хотя raw exception должен оставаться закрытым.
# SOLUTION: library-формулировка несёт process-local маркер PublicError;
# сырой bootstrap/error остаётся отдельно и не расширяет публичный ответ.
# RESULT: production Worker -> Api -> GET/SSE сохраняет причину и границу
# приватности, включая отсутствие ключа на старте и закрытый raw traceback.
# STATUS: FIXED — see #611
def check_workflow_public_errors_611(c):
    import threading
    from mop.common import ops
    from mop.server import opworker

    origin = "git@example.test:g/demo.git"
    who = ("alice", "admin")
    original_add = ops.add

    def exercise(transport, registry, expected, patch_add=False):
        worker = opworker.Worker(registry=lambda: registry, workspace=lambda origin: ("", None))
        api = adminapi.Api(adminapi.Operations(), origin="https://pool.example",
                           executor=lambda action, target, params, who:
                           worker.run(action, target, params, who[0], transport=transport))
        def add_without_wait(*args, **kwargs):
            return original_add(*args, **dict(kwargs,
                place_wait=0 if patch_add == "placement" else 10, bootstrap_wait=0))

        frames, finished = [], threading.Event()
        api.on_terminal(lambda frame: (frames.append(frame), finished.set()))
        with patched(ops, add=add_without_wait if patch_add else original_add):
            rec = api.submit("add", None, {"origin": origin}, who)
            c.check("#611 the operation terminates", finished.wait(5))
            result = api.result(rec["id"], who)
        c.expect("#611 GET preserves its safe workflow reason", result.get("error"), expected)
        c.expect("#611 SSE preserves the same safe workflow reason", frames[0].get("error"), expected)
        body = json.dumps((result, frames))
        c.check("#611 raw project/transport secrets stay outside GET and SSE",
                all(secret not in body for secret in
                    ("PRIVATE-TOKEN", "/private/secrets.env", "PRIVATE-TASK", "PRIVATE-PAYLOAD")), body)

    exercise(lambda *args, **kwargs: {}, set(),
             "project is not registered in the pool; use mop project add")
    exercise(lambda verb, **kwargs: {"name": "pu-demo-7"} if verb == "add" else {},
             {origin}, "pu-demo-7 is not placed on any node after 2 minutes: mop server doctor", "placement")

    def failed_bootstrap(verb, **kwargs):
        if verb == "add":
            return {"name": "pu-demo-7", "bootstrap_marker": "marker"}
        if verb == "alloc":
            return {"alloc": {"ClientStatus": "running", "NodeName": "node-a"}}
        if verb == "bootstrap_result":
            return {"result": {"sent": "marker", "played": True, "ok": False,
                               "task": "PRIVATE-TASK", "message": "PRIVATE-TOKEN /private/secrets.env"}}
        return {}

    exercise(failed_bootstrap, {origin},
             "project bootstrap failed; inspect the puppet status for the failing task")
    exercise(lambda verb, **kwargs: {"name": "pu-demo-7", "bootstrap_marker": "marker"}
             if verb == "add" else {"alloc": {"ClientStatus": "running"}}, {origin},
             "project bootstrap did not finish within the observation deadline", "bootstrap")
    def missing_key(verb, **kwargs):
        if verb == "add":
            return {"name": "pu-demo-7"}
        return {"alloc": {"ClientStatus": "failed", "NodeName": "node-a",
                          "reason": "no MOP_PROXY_KEY in /private/secrets.env",
                          "task": {"state": "dead", "failed": True, "exit": 1,
                                   "restarts": 0, "next_s": None}}}

    exercise(missing_key, {origin},
             "LLM proxy key is missing on the puppet; the cluster service must deliver it")
    c.expect("#611 an unmarked phrase is not trusted just because it sounds safe",
             adminapi.result_error("refused", "project bootstrap failed: PRIVATE-TOKEN"),
             "operation refused")


def main():
    c = Checks()
    who = ("alice", "admin")
    cases = (
        ("failed", "token=private-key /root/secret.env raw payload", "operation failed"),
        ("refused", "pu-mop-1: work in the clone (led by private-user)", "target has unfinished work"),
        ("refused", "pu-mop-1: led by private-user", "target is owned by another user"),
        ("refused", "cannot inspect the target before sending", "target state is unavailable"),
        ("unknown", "message delivery could not be confirmed", "message delivery could not be confirmed"),
        ("partial", "raw payload private-key", "operation completed only partially"),
    )
    for status, error, public in cases:
        api = adminapi.Api(adminapi.Operations(),
                           origin="https://pool.example",
                           executor=lambda *a, e=effect(status, error): e)
        rec = api.submit("send", "pu-mop-1", {"message": "raw payload"}, who)
        got = api.result(rec["id"], who)
        c.expect(f"#495 {status} has a safe public reason", got.get("error"), public)
        body = json.dumps(got)
        c.check("#495 private details never ride the result", all(secret not in body for secret in
                ("private-key", "private-user", "/root", "secret.env", "raw payload", "params")), got)
        c.expect("#495 role gate still hides the result", api.result(rec["id"], ("bob", "user")), {"status": "403"})
    api = adminapi.Api(adminapi.Operations(), origin="https://pool.example",
                       executor=lambda *a: {"outcome": "registered"})
    rec = api.submit("send", "pu-mop-1", {"message": "hello"}, who)
    got = api.result(rec["id"], who)
    c.check("#495 success does not acquire a stale error", "error" not in got, got)
    check_workflow_public_errors_611(c)
    return c.report("web_errors")


if __name__ == "__main__":
    sys.exit(main())
