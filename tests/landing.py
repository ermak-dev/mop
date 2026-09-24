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
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from mop import cluster  # noqa: E402

try:
    from mop import landing  # noqa: E402
except ImportError:
    landing = None

T1, T2 = "2026-09-24T10:00:00Z", "2026-09-24T10:05:00Z"


def check_take_give():
    """CAS: свободен -> взят; чужой -> отказ с держателем; свой -> ok."""
    if landing is None:
        return ["mop.landing is missing"]
    out = []
    tokens, got = landing.take({}, "rugent", "anton", "pu-rugent-1", T1)
    held = tokens.get("rugent") or {}
    if not got.get("ok") or held.get("holder") != "anton" \
            or held.get("puppet") != "pu-rugent-1" or held.get("since") != T1:
        out.append(f"take of a free token must hold it: {got!r}, {tokens!r}")

    # Другой мастер: отказ называет держателя, папета и с какого времени --
    # иначе ему не с кем договориться и нечем решить, ушёл ли тот.
    after, got = landing.take(tokens, "rugent", "boris", "pu-rugent-2", T2)
    why = got.get("error") or ""
    if got.get("ok") or after != tokens:
        out.append(f"take by another master must be refused and change nothing: {got!r}")
    for word in ("anton", "pu-rugent-1", T1):
        if word not in why:
            out.append(f"the refusal must name {word}: {why!r}")

    # Тот же держатель с тем же папетом -- повтор, не отказ: ответ потерялся,
    # мастер спрашивает снова.
    again, got = landing.take(tokens, "rugent", "anton", "pu-rugent-1", T2)
    if not got.get("ok") or again["rugent"].get("since") != T1:
        out.append(f"take again by the holder must pass and keep since: {got!r}, {again!r}")

    # Тот же мастер, но другой папет: токен -- ровно один папет, и выдать
    # его второму, не забрав у первого, значит снова две посадки разом.
    _, got = landing.take(tokens, "rugent", "anton", "pu-rugent-3", T2)
    if got.get("ok") or "pu-rugent-1" not in (got.get("error") or ""):
        out.append(f"the holder's take for a second puppet must be refused: {got!r}")

    # Токен -- на проект: чужой проект его не видит и не ждёт.
    other, got = landing.take(tokens, "mop", "boris", "pu-mop-1", T2)
    if not got.get("ok") or other.get("rugent") != tokens["rugent"]:
        out.append(f"another project's token must be independent: {got!r}, {other!r}")

    # give: только держатель.
    kept, got = landing.give(tokens, "rugent", "boris")
    if got.get("ok") or kept != tokens or "anton" not in (got.get("error") or ""):
        out.append(f"give by a non-holder must be refused naming the holder: {got!r}")
    freed, got = landing.give(tokens, "rugent", "anton")
    if not got.get("ok") or "rugent" in freed:
        out.append(f"give by the holder must free the token: {got!r}, {freed!r}")
    return out


def check_force():
    """force забирает у ушедшего мастера и называет, у кого."""
    if landing is None:
        return ["mop.landing is missing"]
    out = []
    tokens, _ = landing.take({}, "rugent", "anton", "pu-rugent-1", T1)
    taken, got = landing.take(tokens, "rugent", "boris", "pu-rugent-2", T2, force=True)
    prev = got.get("previous") or {}
    if not got.get("ok") or taken["rugent"].get("holder") != "boris" \
            or taken["rugent"].get("since") != T2:
        out.append(f"force take must hand the token over: {got!r}, {taken!r}")
    if prev.get("holder") != "anton" or prev.get("puppet") != "pu-rugent-1":
        out.append(f"force take must name the previous holder: {got!r}")
    freed, got = landing.give(tokens, "rugent", "boris", force=True)
    if not got.get("ok") or "rugent" in freed \
            or (got.get("previous") or {}).get("holder") != "anton":
        out.append(f"force give must free the token naming the holder: {got!r}")
    return out


def check_persistence():
    """Файл сервиса переживает рестарт: запись -> чтение -- то же."""
    if landing is None:
        return ["mop.landing is missing"]
    out = []
    d = tempfile.mkdtemp(prefix="mop-test-landing-")
    path = os.path.join(d, "landing.json")
    if landing.read(path) != {}:
        out.append("no file must read as no tokens")
    tokens, _ = landing.take({}, "rugent", "anton", "pu-rugent-1", T1)
    landing.write(tokens, path)
    if landing.read(path) != tokens:
        out.append(f"round-trip: {landing.read(path)!r}, wanted {tokens!r}")
    with open(path, "w") as f:
        f.write("{broken")
    if landing.read(path) != {}:
        out.append("a broken file must read as no tokens, not crash the verb")
    return out


def check_verb():
    """Глагол сервиса: права, проект из субъекта, файл сервиса."""
    if landing is None:
        return ["mop.landing is missing"]
    from cli import no_network
    out = []
    if "landing" not in cluster.PROJECT_VERBS:
        return out + ["landing must be a project verb of the cluster service"]
    for who in ("rugent", "admin"):
        why = cluster.refusal(who, "landing")
        if why is not None:
            out.append(f"landing must pass for {who}: {why}")
    d = tempfile.mkdtemp(prefix="mop-test-landing-")
    keep = landing.FILE
    undo = no_network()
    try:
        landing.FILE = os.path.join(d, "landing.json")

        def ask(subject, **req):
            return cluster.answer(subject, {"verb": "landing", **req})

        got = ask("rugent", action="take", holder="anton", puppet="pu-rugent-1")
        if not got.get("ok"):
            out.append(f"take through the verb: {got!r}")
        with open(landing.FILE) as f:
            on_disk = json.load(f)
        if (on_disk.get("rugent") or {}).get("holder") != "anton":
            out.append(f"the verb must keep the token in the service's file: {on_disk!r}")
        # Проект -- из субъекта, а не из тела: поле подделывает кто угодно.
        got = ask("mop", action="take", holder="boris", puppet="pu-mop-1",
                  project="rugent")
        if not got.get("ok") or "mop" not in landing.read(landing.FILE):
            out.append(f"a project's subject must decide the project, not the body: {got!r}")
        got = ask("rugent", action="take", holder="boris", puppet="pu-rugent-2")
        if "anton" not in (got.get("error") or ""):
            out.append(f"a second master through the verb must be refused: {got!r}")
        got = ask("rugent", action="show")
        if (got.get("token") or {}).get("holder") != "anton":
            out.append(f"show must tell the holder: {got!r}")
        # Держатель обязателен: безымянный токен некому отдать и не у кого
        # спросить.
        got = ask("rugent", action="take", puppet="pu-rugent-2")
        if not got.get("error"):
            out.append(f"take without a holder must be refused: {got!r}")
        got = ask("rugent", action="nonsense", holder="anton")
        if not got.get("error"):
            out.append(f"an unknown action must be refused: {got!r}")
        # Оператор на своём субъекте называет проект полем: у admin
        # токена нет, он -- у проектов.
        got = ask("admin", action="show", project="rugent")
        if (got.get("token") or {}).get("holder") != "anton":
            out.append(f"the operator must see a project's token: {got!r}")
        got = ask("admin", action="show")
        if not got.get("error"):
            out.append(f"the operator without a project must be refused: {got!r}")
        got = ask("rugent", action="give", holder="anton")
        if not got.get("ok") or "rugent" in landing.read(landing.FILE):
            out.append(f"give through the verb must free the token: {got!r}")
    finally:
        landing.FILE = keep
        undo()
    return out


def main():
    failed = []
    for check in (check_take_give, check_force, check_persistence, check_verb):
        for line in check():
            failed.append(f"FAIL {check.__name__}: {line}")
    if failed:
        print("\n".join(failed))
    print("landing: FAILED" if failed else "landing: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
