#!/usr/bin/env python3
"""Приём #499: идемпотентность в памяти, конфликтов-отказов нет.

HYPOTHESIS: _active-замок отказывал второй операции на цель 409, и отказ
висел часами (#497); завершённая операция не освобождала цель, а очередь
адаптера жила своей жизнью beside воркера.
SOLUTION: приём -- словарь в памяти адаптера: тот же payload -> тот же id;
другой payload -- вторая операция, исполнитель ждёт семафор цели;
завершённая операция освобождает цель и ключ, новый идентичный payload --
снова новая операция (явная команда, не дубликат клика).
"""
import os
import sys
import threading
import time

import hermetic  # noqa: F401
from _lib import Checks
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop.server import adminapi


WHO = ("alice", "admin")
TERMINAL = frozenset(("succeeded", "refused", "failed", "partial", "unknown"))


def fresh():
    """Api с эффектом, который жив, пока проверка его не отпустит:
    «живая» операция -- не гонка потока, а явное состояние."""
    gate = threading.Event()

    def effect(action, target, params, who):
        gate.wait(5)
        return {"outcome": "registered"}
    api = adminapi.Api(adminapi.Operations(), origin="https://pool.example",
                       who=WHO, executor=effect)
    return api, gate


def settled(api, op_id, who=WHO, timeout=5):
    """Дождаться терминала записи (поток исполнения асинхронен)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = api.result(op_id, who)
        if got.get("status") in TERMINAL:
            return got
        time.sleep(0.02)
    return api.result(op_id, who)


def submit(api, message="hello", who=WHO):
    return api.submit("send", "pu-mop-1", {"message": message}, who)


def main():
    c = Checks()
    for status in ("succeeded", "refused", "failed", "partial", "unknown"):
        api, gate = fresh()
        first = submit(api)
        c.expect(f"#499 pending duplicate stays one intent before {status}",
                 submit(api).get("id"), first["id"])
        # Дубликат не поднял второй поток и не звал эффект дважды:
        # в записи -- всё ещё один владелец ключа.
        api._records[first["id"]]["status"] = status
        with_id = api._records[first["id"]]
        c.check(f"#499 the duplicate added no second record for {status}",
                len(api._records) == 1, list(api._records))
        gate.set()

    # Завершённая операция не держит цель: следующий клик -- новая.
    api, gate = fresh()
    first = submit(api)
    gate.set()
    settled(api, first["id"])
    second = submit(api, "next")
    c.check("#499 a terminal release frees the target for a new payload",
            second.get("id") and second["id"] != first["id"], second)
    # Тот же payload после терминала -- тоже новая: дедуп -- про живой
    # клик (двойной щелчок), не про историю.
    api, gate = fresh()
    first = submit(api)
    gate.set()
    settled(api, first["id"])
    again = submit(api)
    c.check("#499 an identical payload after a terminal is a new operation",
            again.get("id") != first["id"], again)

    # Другая цель -- свой id, цели друг другу не мешают.
    api, gate = fresh()
    a = api.submit("add", None, {"origin": "git@example.test:g/a.git"}, WHO)
    b = api.submit("add", None, {"origin": "git@example.test:g/b.git"}, WHO)
    a2 = api.submit("add", None, {"origin": "git@example.test:g/a.git"}, WHO)
    c.expect("#499 targetless duplicate uses the same effective target",
             a2["id"], a["id"])
    c.check("#499 different effective targets do not shadow each other",
            a["id"] != b["id"], (a, b))
    # Другой админ с тем же payload -- тот же id: идемпотентность ключа --
    # payload, а не человек.
    api, gate = fresh()
    mine = submit(api, who=WHO)
    theirs = submit(api, who=("bob", "admin"))
    c.check("#499 the idempotency key is the payload, not the person",
            mine["id"] == theirs["id"], (mine, theirs))
    gate.set()
    return c.report("web_intents")


if __name__ == "__main__":
    sys.exit(main())
