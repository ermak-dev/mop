#!/usr/bin/env python3
"""Проверка снимка пула для дашборда без кластера: python3 tests/web.py

Дашборд (#66) — ещё один фронтенд над теми же данными, что `mop list`,
`mop node` и `mop doctor`. Его единственная логика — как строки папетов
раскладываются по проектам и в какую из пяти корзин попадает папет
(free/busy/sick/silent/down): на корзинах стоят счётчики в шапке страницы,
и здесь они проверяются по виду вердикта (State.kind), как и is_free.
Сборщик, HTTP и страница проверяются только на живом пуле.

STATUS: FIXED — see #66
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.server import web  # noqa: E402
from mop.common.state import PuppetRow, State, silent  # noqa: E402

FREE = State("free", branch="master")


def row(name, origin, state=FREE, alloc="running", node="mate"):
    """Строка puppet_rows: вердикт State, либо None там, где спрашивать некого."""
    return PuppetRow(name=name, node=node, alloc_status=alloc,
                     state=str(state) if state else "-", kind=state and state.kind,
                     owner="-", llm="claude", origin=origin, disk_kb=None)


MOP = "git@git.ermak.dev:ermak/mop.git"
RUGENT = "git@git.ermak.dev:rugent/rugent.git"

# (строка, корзина). Корзина — вердикт дашборда поверх вида вердикта: строка
# состояния для человека, а счётчик в шапке считается по виду и одинаков для
# «HUNG (no tmux session)» и «HUNG (not responding)» (#145).
CLASSIFY = [
    (row("pu-mop-1", MOP, FREE), "free"),
    (row("pu-mop-2", MOP, State("free", branch="feat/66")), "free"),
    (row("pu-mop-3", MOP, State("busy", branch="feat/66")), "busy"),
    (row("pu-mop-4", MOP, State("waiting")), "busy"),
    (row("pu-mop-5", MOP, State("hung", "not responding")), "sick"),
    (row("pu-mop-6", MOP, State("dialog", "dialog")), "sick"),
    (row("pu-mop-7", MOP, State("login", "not logged in")), "sick"),
    (row("pu-mop-8", MOP, State("login", "login expired", "feat/66")), "sick"),
    (row("pu-mop-9", MOP, State("quota")), "sick"),
    (row("pu-mop-10", MOP, State("error", "Connection error")), "sick"),
    (row("pu-mop-11", MOP, silent("no responders")), "silent"),
    # Аллокация не бежит — состояния спрашивать не у кого, и это «down»
    # независимо от того, что в колонке состояния (там прочерк).
    (row("pu-mop-12", MOP, None, alloc="pending", node="-"), "down"),
    (row("pu-mop-13", MOP, None, alloc="lost"), "down"),
    (row("pu-mop-14", MOP, None, alloc="dead"), "down"),
    # Ошибка ростера (Nomad не ответил про аллокацию) — тоже down.
    (row("pu-mop-15", MOP, None, alloc="no connection to nomad"), "down"),
]

ROWS = [
    row("pu-rugent-2", RUGENT, State("busy", branch="feat/12"), node="hyper"),
    row("pu-mop-1", MOP, FREE),
    row("pu-rugent-1", RUGENT, None, alloc="pending", node="-"),
    row("pu-mop-2", MOP, silent("no responders"), node="gpu"),
    # Неизвестный origin — своя группа, а не падение на разборе имени.
    row("pu-what-1", "?", FREE),
]


def check_classify():
    failed = 0
    for r, want in CLASSIFY:
        got = web.classify(r)
        if got != want:
            failed += 1
            print(f"FAIL classify({r.name}: {r.state!r}/{r.alloc_status}): "
                  f"{got} != {want}")
    return failed


def check_projects():
    failed = 0
    got = web.projects(ROWS)
    names = [s["name"] for s in got]
    # По имени проекта, папеты внутри — по имени: порядок на странице стабилен,
    # а puppet_rows_stream отдаёт строки по готовности.
    if names != ["?", "mop", "rugent"]:
        failed += 1
        print(f"FAIL projects order: {names}")
    by = {s["name"]: s for s in got}
    if [p["name"] for p in by["rugent"]["puppets"]] != ["pu-rugent-1", "pu-rugent-2"]:
        failed += 1
        print(f"FAIL puppets order in rugent: {[p['name'] for p in by['rugent']['puppets']]}")
    want = {"puppets": 2, "free": 1, "busy": 0, "sick": 0, "silent": 1, "down": 0}
    if by["mop"]["counts"] != want:
        failed += 1
        print(f"FAIL counts(mop): {by['mop']['counts']} != {want}")
    # Корзина лежит в строке: страница красит по ней, а не разбирает
    # текст состояния второй раз на JS.
    if by["rugent"]["puppets"][0].get("kind") != "down":
        failed += 1
        print(f"FAIL kind on row: {by['rugent']['puppets'][0]}")
    total = web.counts(ROWS)
    want = {"puppets": 5, "free": 2, "busy": 1, "sick": 0, "silent": 1, "down": 1}
    if total != want:
        failed += 1
        print(f"FAIL counts(all): {total} != {want}")
    return failed


def check_sizes():
    """Обмер приезжает отдельным поездом и вливается в строки по имени; кого
    не обмерили — прочерк (None), а не ноль и не потеря строки."""
    failed = 0
    got = web.with_sizes(ROWS[:2], {"pu-mop-1": 2048})
    kb = {r.name: r.disk_kb for r in got}
    if kb != {"pu-rugent-2": None, "pu-mop-1": 2048}:
        failed += 1
        print(f"FAIL with_sizes: {kb}")
    # Исходные строки не трогаем: сборщик держит их между поездами.
    if ROWS[1].disk_kb is not None:
        failed += 1
        print("FAIL with_sizes mutated its input")
    return failed


def check_journal():
    """Журнал событий — кольцо: старое вытесняется, порядок от старого к
    новому, вход не мутируется."""
    failed = 0
    log = []
    for i in range(5):
        log = web.push(log, {"event": "send", "n": i}, cap=3)
    if [e["n"] for e in log] != [2, 3, 4]:
        failed += 1
        print(f"FAIL push ring: {[e['n'] for e in log]}")
    return failed


def check_usage():
    """Ось графика — все дни окна, включая пустые: провал виден на месте."""
    failed = 0
    from datetime import datetime
    now = datetime(2026, 9, 22, 12, 0)
    per_day = {"2026-09-22": {"input": 10, "output": 5, "cache_write": 0, "cache_read": 100},
               "2026-09-20": {"input": 1, "output": 1, "cache_write": 1, "cache_read": 1}}
    got = web.usage_axis(per_day, 3, now=now)
    want = [{"date": "2026-09-20", "total": 4, "input": 1, "output": 1,
             "cache_write": 1, "cache_read": 1},
            {"date": "2026-09-21", "total": 0, "input": 0, "output": 0,
             "cache_write": 0, "cache_read": 0},
            {"date": "2026-09-22", "total": 115, "input": 10, "output": 5,
             "cache_write": 0, "cache_read": 100}]
    if got != want:
        failed += 1
        print(f"FAIL usage_axis: {got}")
    return failed


def check_snapshot():
    """Снимок — один JSON для страницы и /api/pool: всё, что в нём лежит,
    страница читает по имени, поэтому набор ключей закреплён."""
    failed = 0
    snap = web.snapshot(rows=ROWS, nodes=[{"name": "mate"}],
                        usage=[], per_puppet=[], per_user=[], journal=[], errors=["bus: down"],
                        at=1_000_000.0)
    # per_user -- расход по людям (#245).
    want = {"at", "projects", "counts", "nodes", "usage", "per_puppet", "per_user",
            "journal", "errors"}
    if set(snap) != want:
        failed += 1
        print(f"FAIL snapshot keys: {sorted(set(snap) ^ want)}")
    if snap["counts"]["puppets"] != 5 or snap["errors"] != ["bus: down"]:
        failed += 1
        print(f"FAIL snapshot body: {snap['counts']} {snap['errors']}")
    return failed


def check_by_user_245():
    """HYPOTHESIS (#245): оператор видит расход по папетам и по дням, но не
    по людям -- кто сколько тратит, не видно. #244 учит глагол usage агента
    класть рядом со старым полем usage новое by_login:
    {папет: {логин: {дата: {вид: n}}}}, "-" -- неприписанное.
    SOLUTION: чистая свёртка ответов узлов в строки по людям, от самого
    прожорливого; узел со старым агентом (без by_login) -- не ошибка: весь
    расход его папетов -- «-». Сумма по людям равна сумме по папетам.
    STATUS: FIXED — see #245"""
    failed = 0
    fn = getattr(web, "user_rows", None)
    if fn is None:
        print("FAIL #245: web.user_rows is missing")
        return 1

    def u(i, o, w, r):
        return {"input": i, "output": o, "cache_write": w, "cache_read": r}
    answers = {
        # Новый агент: у каждого папета расход разложен по логинам.
        "hyper": {"ok": True,
                  "usage": {"pu-mop-1": {"2026-09-22": u(10, 5, 0, 100), "2026-09-21": u(1, 1, 1, 1)},
                            "pu-mop-2": {"2026-09-22": u(4, 4, 4, 4)}},
                  "by_login": {"pu-mop-1": {"anton": {"2026-09-22": u(10, 5, 0, 100)},
                                            "-": {"2026-09-21": u(1, 1, 1, 1)}},
                               "pu-mop-2": {"ivan": {"2026-09-22": u(4, 4, 4, 4)}}}},
        # Старый агент: by_login нет -- весь расход в «-».
        "gpu": {"ok": True, "usage": {"pu-mop-3": {"2026-09-22": u(2, 0, 0, 0)}}},
        # Новый агент, но папета нет в by_login -- его расход тоже «-».
        "mini": {"ok": True, "usage": {"pu-web-1": {"2026-09-22": u(0, 3, 0, 0)}},
                 "by_login": {}},
        # Узел не ответил -- его нет ни в одной строке.
        "dead": {"error": "agent silent"},
    }
    got = fn(answers)
    want = [{"login": "anton", "input": 10, "output": 5, "cache_write": 0, "cache_read": 100, "total": 115},
            {"login": "ivan", "input": 4, "output": 4, "cache_write": 4, "cache_read": 4, "total": 16},
            {"login": "-", "input": 3, "output": 4, "cache_write": 1, "cache_read": 1, "total": 9}]
    if got != want:
        failed += 1
        print(f"FAIL #245 user_rows: {got}")
    # Инвариант: по людям -- столько же, сколько по папетам.
    puppets_total = sum(sum(sum(r.values()) for r in rows.values())
                        for a in answers.values() for rows in (a.get("usage") or {}).values())
    if sum(r["total"] for r in got) != puppets_total:
        failed += 1
        print(f"FAIL #245 invariant: users {sum(r['total'] for r in got)} != puppets {puppets_total}")
    snap = web.snapshot(rows=[], nodes=[], usage=[], per_puppet=[], per_user=got,
                        journal=[], errors=[], at=1.0)
    if snap.get("per_user") != got:
        failed += 1
        print(f"FAIL #245 snapshot per_user: {snap.get('per_user')}")
    return failed


def check_sick_in_project_210():
    """HYPOTHESIS (#210): счётчик проекта считается по строкам, у которых
    kind уже заменён корзиной, а classify("sick") отвечает busy: больной
    папет попадает в проект занятым, хотя шапка считает верно -- оператор
    видит здоровый проект, когда папет в нём залип.
    SOLUTION: корзина проекта -- тот же classify по виду вердикта, что и
    шапка; в строку страницы корзина кладётся только при сериализации.
    STATUS: FIXED — see #210"""
    failed = 0
    rows = [row("pu-mop-1", MOP, State("hung", "not responding")),
            row("pu-mop-2", MOP, State("busy", branch="feat/210"))]
    snap = web.snapshot(rows=rows, nodes=[], usage=[], per_puppet=[], per_user=[], journal=[],
                        errors=[], at=1.0)
    want = {"puppets": 2, "free": 0, "busy": 1, "sick": 1, "silent": 0, "down": 0}
    if snap["counts"] != want:
        failed += 1
        print(f"FAIL #210 header counts: {snap['counts']} != {want}")
    got = snap["projects"][0]["counts"]
    if got != want:
        failed += 1
        print(f"FAIL #210 project counts: {got} != {want}")
    kinds = [p["kind"] for p in snap["projects"][0]["puppets"]]
    if kinds != ["sick", "busy"]:
        failed += 1
        print(f"FAIL #210 bucket on rows: {kinds}")
    return failed


def main():
    failed = (check_classify() + check_projects() + check_sizes()
              + check_journal() + check_usage() + check_snapshot()
              + check_sick_in_project_210() + check_by_user_245())
    print("web: FAILED" if failed else "web: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
