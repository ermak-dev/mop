#!/usr/bin/env python3
"""Токен посадки без пула: python3 tests/landing.py

Посадка -- ровно один папет между merge и push на проект: два гейта
параллельно гоняются друг с другом, и второй push отскакивает
non-fast-forward, прогнав гейт по интеграции, которой уже нет.

HYPOTHESIS (#42): токен жил только в голове мастера ("The queue is yours --
no lock file, no extra tooling"). Два мастера одного проекта -- две головы, и
токена не существует вовсе: посадки гоняются.
SOLUTION: глагол `landing` у сервиса кластера, по проекту из субъекта: take
-- CAS (свободен или уже у того же держателя с тем же папетом, иначе отказ с
держателем и временем), give -- только держатель, force -- забрать у
ушедшего мастера, назвав, у кого. Хранится файлом сервиса на сервере рядом с
лимитами и переживает его рестарт.
RESULT: CAS, force и файл -- здесь; глагол через cluster.answer без сети.
STATUS: FIXED — see #42
"""
import json
import os
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, offline, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from mop.server import cluster  # noqa: E402

try:
    from mop.common import landing  # noqa: E402
except ImportError:
    landing = None

T1, T2 = "2026-09-24T10:00:00Z", "2026-09-24T10:05:00Z"


def check_take_give(c):
    """CAS: свободен -> взят; чужой -> отказ с держателем; свой -> ok."""
    if landing is None:
        c.fail("check_take_give: mop.landing is missing")
        return
    tokens, got = landing.take({}, "rugent", "anton", "pu-rugent-1", T1)
    held = tokens.get("rugent") or {}
    c.check("take of a free token must hold it",
            not (not got.get("ok") or held.get("holder") != "anton"
                 or held.get("puppet") != "pu-rugent-1" or held.get("since") != T1),
            f"{got!r}, {tokens!r}")

    # Другой мастер: отказ называет держателя, папета и с какого времени --
    # иначе ему не с кем договориться и нечем решить, ушёл ли тот.
    after, got = landing.take(tokens, "rugent", "boris", "pu-rugent-2", T2)
    why = got.get("error") or ""
    c.check("take by another master must be refused and change nothing",
            not (got.get("ok") or after != tokens), repr(got))
    for word in ("anton", "pu-rugent-1", T1):
        c.check(f"the refusal must name {word}", not (word not in why), repr(why))

    # Тот же держатель с тем же папетом -- повтор, не отказ: ответ потерялся,
    # мастер спрашивает снова.
    again, got = landing.take(tokens, "rugent", "anton", "pu-rugent-1", T2)
    c.check("take again by the holder must pass and keep since",
            not (not got.get("ok") or again["rugent"].get("since") != T1),
            f"{got!r}, {again!r}")

    # Тот же мастер, но другой папет: токен -- ровно один папет, и выдать
    # его второму, не забрав у первого, значит снова две посадки разом.
    _, got = landing.take(tokens, "rugent", "anton", "pu-rugent-3", T2)
    c.check("the holder's take for a second puppet must be refused",
            not (got.get("ok") or "pu-rugent-1" not in (got.get("error") or "")), repr(got))

    # Токен -- на проект: чужой проект его не видит и не ждёт.
    other, got = landing.take(tokens, "mop", "boris", "pu-mop-1", T2)
    c.check("another project's token must be independent",
            not (not got.get("ok") or other.get("rugent") != tokens["rugent"]),
            f"{got!r}, {other!r}")

    # give: только держатель.
    kept, got = landing.give(tokens, "rugent", "boris")
    c.check("give by a non-holder must be refused naming the holder",
            not (got.get("ok") or kept != tokens or "anton" not in (got.get("error") or "")),
            repr(got))
    freed, got = landing.give(tokens, "rugent", "anton")
    c.check("give by the holder must free the token",
            not (not got.get("ok") or "rugent" in freed), f"{got!r}, {freed!r}")


def check_force(c):
    """force забирает у ушедшего мастера и называет, у кого."""
    if landing is None:
        c.fail("check_force: mop.landing is missing")
        return
    tokens, _ = landing.take({}, "rugent", "anton", "pu-rugent-1", T1)
    taken, got = landing.take(tokens, "rugent", "boris", "pu-rugent-2", T2, force=True)
    prev = got.get("previous") or {}
    c.check("force take must hand the token over",
            not (not got.get("ok") or taken["rugent"].get("holder") != "boris"
                 or taken["rugent"].get("since") != T2),
            f"{got!r}, {taken!r}")
    c.check("force take must name the previous holder",
            not (prev.get("holder") != "anton" or prev.get("puppet") != "pu-rugent-1"),
            repr(got))
    freed, got = landing.give(tokens, "rugent", "boris", force=True)
    c.check("force give must free the token naming the holder",
            not (not got.get("ok") or "rugent" in freed
                 or (got.get("previous") or {}).get("holder") != "anton"),
            repr(got))


def check_persistence(c):
    """Файл сервиса переживает рестарт: запись -> чтение -- то же."""
    if landing is None:
        c.fail("check_persistence: mop.landing is missing")
        return
    d = tempfile.mkdtemp(prefix="mop-test-landing-")
    path = os.path.join(d, "landing.json")
    c.expect("no file must read as no tokens", landing.read(path), {})
    tokens, _ = landing.take({}, "rugent", "anton", "pu-rugent-1", T1)
    landing.write(tokens, path)
    c.expect("round-trip", landing.read(path), tokens)
    with open(path, "w") as f:
        f.write("{broken")
    c.expect("a broken file must read as no tokens, not crash the verb", landing.read(path), {})


def check_verb(c):
    """Глагол сервиса: права, проект из субъекта, файл сервиса."""
    if landing is None:
        c.fail("check_verb: mop.landing is missing")
        return
    if not c.check("landing must be a project verb of the cluster service",
                   not ("landing" not in cluster.PROJECT_VERBS)):
        return
    for who in ("rugent", "admin"):
        why = cluster.refusal(who, "landing")
        c.check(f"landing must pass for {who}", not (why is not None), why)
    d = tempfile.mkdtemp(prefix="mop-test-landing-")
    with offline(), patched(landing, FILE=os.path.join(d, "landing.json")):

        def ask(subject, **req):
            return cluster.answer(subject, {"verb": "landing", **req})

        got = ask("rugent", action="take", holder="anton", puppet="pu-rugent-1")
        c.check("take through the verb", not (not got.get("ok")), repr(got))
        with open(landing.FILE) as f:
            on_disk = json.load(f)
        c.check("the verb must keep the token in the service's file",
                not ((on_disk.get("rugent") or {}).get("holder") != "anton"), repr(on_disk))
        # Проект -- из субъекта, а не из тела: поле подделывает кто угодно.
        got = ask("mop", action="take", holder="boris", puppet="pu-mop-1",
                  project="rugent")
        c.check("a project's subject must decide the project, not the body",
                not (not got.get("ok") or "mop" not in landing.read(landing.FILE)), repr(got))
        got = ask("rugent", action="take", holder="boris", puppet="pu-rugent-2")
        c.check("a second master through the verb must be refused",
                not ("anton" not in (got.get("error") or "")), repr(got))
        got = ask("rugent", action="show")
        c.check("show must tell the holder",
                not ((got.get("token") or {}).get("holder") != "anton"), repr(got))
        # Держатель обязателен: безымянный токен некому отдать и не у кого
        # спросить.
        got = ask("rugent", action="take", puppet="pu-rugent-2")
        c.check("take without a holder must be refused", not (not got.get("error")), repr(got))
        got = ask("rugent", action="nonsense", holder="anton")
        c.check("an unknown action must be refused", not (not got.get("error")), repr(got))
        # Оператор на своём субъекте называет проект полем: у admin
        # токена нет, он -- у проектов.
        got = ask("admin", action="show", project="rugent")
        c.check("the operator must see a project's token",
                not ((got.get("token") or {}).get("holder") != "anton"), repr(got))
        got = ask("admin", action="show")
        c.check("the operator without a project must be refused",
                not (not got.get("error")), repr(got))
        got = ask("rugent", action="give", holder="anton")
        c.check("give through the verb must free the token",
                not (not got.get("ok") or "rugent" in landing.read(landing.FILE)), repr(got))


def main():
    c = Checks()
    for check in (check_take_give, check_force, check_persistence, check_verb):
        check(c)
    return c.report("landing")


if __name__ == "__main__":
    sys.exit(main())
