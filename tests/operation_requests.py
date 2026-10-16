#!/usr/bin/env python3
"""Повтор запроса и смена эпохи до допуска нового эффекта (#625).

HYPOTHESIS: после restore потерянный ключ или истёкший результат принимаются
за новый запрос; изменившийся payload прячется за истечением результата.
SOLUTION: сначала сверить эпоху, затем сохранённый digest, затем доступность
результата. Только отсутствие ключа в текущей эпохе допускает новую проверку.
RESULT: новая политика прошла после наблюдаемого красного прогона;
применение к уникальному ключу и COMMIT проверяется отдельно в PostgreSQL.
STATUS: FIXED — see #625
"""
import importlib
import importlib.util
import os
import sys
from uuid import UUID

import hermetic  # noqa: F401
from _lib import Checks

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

EPOCH = UUID("d7ee4c63-2a6b-4e6f-a9cd-c2990bf3a05f")
OLD = UUID("c86c580c-4e49-4d2c-aceb-968d2f504c54")
DIGEST = "a" * 64
OTHER = "b" * 64


def main():
    c = Checks()
    try:
        present = importlib.util.find_spec("mop.server.operations.requests") is not None
    except ModuleNotFoundError:
        present = False
    if not c.check("request replay policy is available", present):
        return c.report("operation_requests")
    requests = importlib.import_module("mop.server.operations.requests")

    def resolve(**changes):
        args = dict(expected_epoch=EPOCH, current_epoch=EPOCH,
                    submitted_digest=DIGEST, stored_digest=None, result_available=True)
        args.update(changes)
        return requests.resolve_retry(**args)

    def refused(what, code, **changes):
        try:
            resolve(**changes)
        except requests.RequestRefused as error:
            c.expect(what, error.code, code)
            c.check(f"{what} hides intent material", DIGEST not in str(error) and OTHER not in str(error))
        else:
            c.fail(what, "request was not refused")

    c.expect("an unseen current-epoch request needs admission", resolve(), requests.Retry.NEW)
    c.expect("a duplicate returns its prior operation", resolve(stored_digest=DIGEST), requests.Retry.REPLAY)
    c.expect("result expiry cannot make a key new",
             resolve(stored_digest=DIGEST, result_available=False), requests.Retry.EXPIRED)
    c.expect("unseen result flag does not create a tombstone",
             resolve(result_available=False), requests.Retry.NEW)
    c.expect("wire UUID normalizes to the same epoch",
             resolve(expected_epoch=str(EPOCH), current_epoch=str(EPOCH)), requests.Retry.NEW)

    for prior in (None, DIGEST, OTHER):
        for available in (True, False):
            refused(f"stale epoch refuses before lookup {prior is None}/{available}",
                    "epoch_conflict", expected_epoch=OLD, stored_digest=prior,
                    result_available=available)
    refused("different intent with the same request key", "request_conflict", stored_digest=OTHER)
    refused("expired result still exposes an intent conflict", "request_conflict",
            stored_digest=OTHER, result_available=False)

    for field in ("expected_epoch", "current_epoch"):
        for value in (None, "", "not-an-epoch", True, 1):
            refused(f"invalid {field} {value!r}", "invalid_epoch", **{field: value})
    for field in ("submitted_digest", "stored_digest"):
        for value in ("", "a" * 63, "g" * 64, "A" * 64, 1, True, [DIGEST]):
            refused(f"invalid {field} {type(value).__name__}/{str(value)[:3]}",
                    "invalid_digest", **{field: value})
    for value in (None, 0, 1, "false"):
        refused(f"invalid result availability {value!r}", "invalid_result", result_available=value)

    return c.report("operation_requests")


if __name__ == "__main__":
    sys.exit(main())
