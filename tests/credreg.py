#!/usr/bin/env python3
"""Реестр кредитов без сервера: python3 tests/credreg.py

Кредит -- именованная авторизация у провайдера LLM (эпик #281). Здесь
проверяется чистая часть реестра (#283): запись кредита, строки для
`mop cred list`, слияние пробы со статусом, решение о продлении токена.
Сеть, pty и шина остаются живому серверу.

HYPOTHESIS: кредита как сущности нет -- логин claude.ai живёт копией файла
оператора по телам, ключ GLM строкой в .env; кому принадлежит, сколько
осталось и когда сброс, не знает никто, и `mop list` узнаёт о беде по
провалу хода.
SOLUTION: mop/common/credreg.py -- запись `cred.json` и её чтение в строки;
mop/server/credreg.py -- дома кредитов на сервере, проба через хук профиля,
продление токена самим клиентом; глаголы cred_* сервиса кластера.
STATUS: FIXED — see #283
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop.common import credreg  # noqa: E402
from mop.common.domain import CredStatus  # noqa: E402

NOW = 1_790_500_000


def main():
    c = Checks()

    # Запись: форма закреплена -- её читают list, дашборд и политика.
    rec = credreg.record("anton", "claude", "login", owner="anton@example.dev", now=NOW)
    c.expect("record: the fixed shape", rec,
             {"name": "anton", "profile": "claude", "kind": "login",
              "owner": "anton@example.dev", "added_at": NOW, "status": None})
    for bad in ("", "a/b", "../x", "with space", ".hidden"):
        try:
            credreg.record(bad, "claude", "login")
            c.fail(f"record: name {bad!r} must refuse -- it becomes a directory")
        except ValueError:
            pass
    try:
        credreg.record("ok", "claude", "session")
        c.fail("record: an unknown kind must refuse")
    except ValueError:
        pass

    # Проба вливается в запись вместе с временем пробы; kind/resets/percent/
    # detail -- ровно поля CredStatus, чтобы страница и политика читали одно.
    probed = credreg.merge_status(rec, CredStatus("quota_wait", resets_at=NOW + 3600,
                                                  percent=100, detail="5h window exhausted"),
                                  now=NOW + 10)
    c.expect("merge_status: the probe lands in the record", probed["status"],
             {"kind": "quota_wait", "resets_at": NOW + 3600, "percent": 100,
              "detail": "5h window exhausted", "probed_at": NOW + 10})
    c.check("merge_status: the input record is untouched", rec["status"] is None)

    # Строки списка: слово статуса, сброс по-человечески, процент, возраст.
    key = credreg.merge_status(credreg.record("z1", "glm", "key", now=NOW - 3 * 86400),
                               CredStatus("active", percent=53, detail="5h 1%, weekly 53%"),
                               now=NOW - 60)
    dead = credreg.merge_status(credreg.record("old", "glm", "key", now=NOW - 40 * 86400),
                                CredStatus("needs_login", detail="HTTP 401"), now=NOW)
    rows = credreg.rows([rec, probed, key, dead], now=NOW + 10)
    c.expect("rows: header first", rows[0],
             ("name", "profile", "kind", "owner", "status", "resets", "used", "age"))
    c.expect("rows: unprobed -> unknown", rows[1],
             ("anton", "claude", "login", "anton@example.dev", "unknown", "-", "-", "0m"))
    c.expect("rows: quota wait with a human reset", rows[2],
             ("anton", "claude", "login", "anton@example.dev", "quota wait", "in 59m", "100%", "0m"))
    c.expect("rows: active key", rows[3],
             ("z1", "glm", "key", "-", "active", "-", "53%", "3d"))
    c.expect("rows: dead key names the reason", rows[4],
             ("old", "glm", "key", "-", "needs login: HTTP 401", "-", "-", "40d"))
    c.expect("rows: nothing -> header only", credreg.rows([], now=NOW), [rows[0]])

    # Сброс в прошлом -- окно уже открылось: показываем «now», а не минус.
    stale = credreg.merge_status(rec, CredStatus("quota_wait", resets_at=NOW - 5), now=NOW)
    c.expect("rows: a reset in the past reads now", credreg.rows([stale], now=NOW)[1][5], "now")

    # Продление токена (#283): claude обновляет токен сам при запросе, и
    # сервер зовёт `claude -p` в доме кредита, пока срок не вышел. Порог --
    # два часа; без срока (ключ, setup-token) продлевать нечего.
    for what, exp, want in [
        ("far away -> no", NOW + 8 * 3600, False),
        ("within two hours -> yes", NOW + 3600, True),
        ("already expired -> yes (refresh token may still work)", NOW - 10, True),
        ("no expiry -> no", None, False),
    ]:
        c.expect(f"needs_keepalive: {what}", credreg.needs_keepalive(exp, NOW), want)

    # Срок из файла кредов claude: expiresAt в миллисекундах.
    c.expect("expires_at: ms -> s", credreg.expires_at(
        {"claudeAiOauth": {"expiresAt": 1790425418306}}), 1790425418)
    c.expect("expires_at: no oauth -> None", credreg.expires_at({}), None)
    c.expect("expires_at: garbage -> None", credreg.expires_at("nope"), None)

    return c.report("credreg")


if __name__ == "__main__":
    sys.exit(main())
