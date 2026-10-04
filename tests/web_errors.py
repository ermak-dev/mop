#!/usr/bin/env python3
"""Публичная причина завершения не раскрывает частные ошибки воркера.

HYPOTHESIS: error записан в settle, но result выбрасывает его целиком.
SOLUTION: публиковать только закрытые фразы, а не сырые исключения,
payload сообщения, пути или ключи из журнала исполнителя.
RESULT: публичный результат несёт только закрытую фразу по причине отказа.
STATUS: FIXED — see #495
"""
import json
import os
import sys

import hermetic  # noqa: F401
from _lib import Checks
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop.server import adminapi


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
        api = adminapi.Api(adminapi.Operations(), adminapi.Queue(), origin="https://pool.example")
        rec = api.submit("send", "pu-mop-1", {"message": "raw payload"}, who)
        api.settle(rec["id"], {"status": status, "error": error})
        got = api.result(rec["id"], who)
        c.expect(f"#495 {status} has a safe public reason", got.get("error"), public)
        body = json.dumps(got)
        c.check("#495 private details never ride the result", all(secret not in body for secret in
                ("private-key", "private-user", "/root", "secret.env", "raw payload", "params")), got)
        c.expect("#495 role gate still hides the result", api.result(rec["id"], ("bob", "user")), {"status": "403"})
    api = adminapi.Api(adminapi.Operations(), adminapi.Queue(), origin="https://pool.example")
    rec = api.submit("send", "pu-mop-1", {"message": "hello"}, who)
    api.settle(rec["id"], {"status": "succeeded", "error": "private-key"})
    c.check("#495 success does not acquire a stale error", "error" not in api.result(rec["id"], who))
    return c.report("web_errors")


if __name__ == "__main__":
    sys.exit(main())
