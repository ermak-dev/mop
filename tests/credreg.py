#!/usr/bin/env python3
"""Проверка записей кредитов без пула: python3 tests/credreg.py

Серверная машина кредитов умерла (#386): реестр, дома, pty-логин и раздача
выпилены. Осталась чистая запись -- её читают ярусы (tiers.usable) до
своего снятия (#390). Здесь -- только выжившее: форма записи, слова
статусов, возраст, usable/pick.
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import credreg  # noqa: E402
from mop.common.domain import CredStatus  # noqa: E402

NOW = 1790000000


def main():
    c = Checks()
    rec = credreg.record("a", "glm", "key", owner="x", now=NOW)
    c.expect("record: the fixed shape", rec,
             {"name": "a", "profile": "glm", "kind": "key", "owner": "x",
              "added_at": NOW, "status": None})
    merged = credreg.merge_status(rec, CredStatus("active", percent=30), NOW)
    c.expect("merge_status: a new record with the probe",
             merged["status"], {"kind": "active", "resets_at": None, "percent": 30,
                                "detail": "", "probed_at": NOW})
    c.expect("status_word: needs_login carries its detail",
             credreg.status_word({"kind": "needs_login", "detail": "expired"},
                                 credreg.WORDS, "?"), "needs login: expired")
    c.expect("age: minutes, hours, days", [credreg.age(merged, NOW + s) for s in
                                           (59, 3600, 3 * 86400)], ["0m", "1h", "3d"])
    recs = [credreg.merge_status(credreg.record(n, "glm", "key", now=NOW), CredStatus(k), NOW)
            for n, k in (("b", "active"), ("a", "active"), ("c", "needs_login"))]
    c.expect("usable: active only, by name", credreg.usable("glm",
             ((r["name"], r["profile"], (r.get("status") or {}).get("kind")) for r in recs)),
             ["a", "b"])
    c.expect("reset_time_of: parses the UTC reset",
             credreg.reset_time_of("Your limit will reset at 2026-09-24 15:00:00"),
             1790262000)
    c.expect("reset_time_of: garbage -> None", credreg.reset_time_of("soon"), None)
    return c.report("credreg")


if __name__ == "__main__":
    sys.exit(main())
