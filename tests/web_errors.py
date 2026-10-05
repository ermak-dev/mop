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
from _lib import Checks
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop.server import adminapi


def effect(status, error):
    """Эффект-подмена: сразу терминальный вердикт #476."""
    if error is None:
        return {"outcome": "registered"}
    kind = {"refused": "refused", "failed": "failed",
            "partial": "partial", "unknown": "unknown"}[status]
    return {"outcome": kind, "error": error}


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
    return c.report("web_errors")


if __name__ == "__main__":
    sys.exit(main())
