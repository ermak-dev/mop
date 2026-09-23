#!/usr/bin/env python3
"""Проверка снимка пула для дашборда без кластера: python3 tests/web.py

Дашборд (#66) — ещё один фронтенд над теми же данными, что `mop list`,
`mop node` и `mop doctor`. Его единственная логика — как строки папетов
раскладываются по проектам и в какую из пяти корзин попадает папет
(free/busy/sick/silent/down): на корзинах стоят счётчики в шапке страницы,
и здесь они проверяются на той же матрице состояний, что и is_free.
Сборщик, HTTP и страница проверяются только на живом пуле.

STATUS: FIXED — see #66
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import web  # noqa: E402


def row(name, origin, state="free (master)", alloc="running", node="mate"):
    return {"name": name, "node": node, "alloc_status": alloc, "state": state,
            "llm": "claude", "origin": origin, "disk_kb": None}


MOP = "git@git.ermak.dev:ermak/mop.git"
RUGENT = "git@git.ermak.dev:rugent/rugent.git"

# (строка, корзина). Корзина — вердикт дашборда поверх состояния мастера:
# состояние там строка для человека, а счётчик в шапке должен считаться
# одинаково для «HUNG 12m» и «HUNG 3h».
CLASSIFY = [
    (row("pu-mop-1", MOP, "free (master)"), "free"),
    (row("pu-mop-2", MOP, "free (feat/66), 2 unpushed"), "free"),
    (row("pu-mop-3", MOP, "busy: working (feat/66)"), "busy"),
    (row("pu-mop-4", MOP, "waiting for input (feat/66)"), "busy"),
    (row("pu-mop-5", MOP, "HUNG 12m (feat/66)"), "sick"),
    (row("pu-mop-6", MOP, "needs action: permission dialog"), "sick"),
    (row("pu-mop-7", MOP, "not logged in"), "sick"),
    (row("pu-mop-8", MOP, "login expired"), "sick"),
    (row("pu-mop-9", MOP, "no model quota"), "sick"),
    (row("pu-mop-10", MOP, "error: API Error 500"), "sick"),
    (row("pu-mop-11", MOP, "AGENT SILENT (no responders)"), "silent"),
    # Аллокация не бежит — состояния спрашивать не у кого, и это «down»
    # независимо от того, что в колонке состояния (там прочерк).
    (row("pu-mop-12", MOP, "-", alloc="pending", node="-"), "down"),
    (row("pu-mop-13", MOP, "-", alloc="lost"), "down"),
    (row("pu-mop-14", MOP, "-", alloc="dead"), "down"),
    # Ошибка ростера (Nomad не ответил про аллокацию) — тоже down.
    (row("pu-mop-15", MOP, "-", alloc="no connection to nomad"), "down"),
]

ROWS = [
    row("pu-rugent-2", RUGENT, "busy: cargo build (feat/12)", node="hyper"),
    row("pu-mop-1", MOP, "free (master)"),
    row("pu-rugent-1", RUGENT, "-", alloc="pending", node="-"),
    row("pu-mop-2", MOP, "AGENT SILENT (no responders)", node="gpu"),
    # Неизвестный origin — своя группа, а не падение на разборе имени.
    row("pu-what-1", "?", "free (master)"),
]


def check_classify():
    failed = 0
    for r, want in CLASSIFY:
        got = web.classify(r)
        if got != want:
            failed += 1
            print(f"FAIL classify({r['name']}: {r['state']!r}/{r['alloc_status']}): "
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
    kb = {r["name"]: r["disk_kb"] for r in got}
    if kb != {"pu-rugent-2": None, "pu-mop-1": 2048}:
        failed += 1
        print(f"FAIL with_sizes: {kb}")
    # Исходные строки не трогаем: сборщик держит их между поездами.
    if ROWS[1]["disk_kb"] is not None:
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
                        usage=[], per_puppet=[], journal=[], errors=["bus: down"],
                        at=1_000_000.0)
    want = {"at", "projects", "counts", "nodes", "usage", "per_puppet",
            "journal", "errors"}
    if set(snap) != want:
        failed += 1
        print(f"FAIL snapshot keys: {sorted(set(snap) ^ want)}")
    if snap["counts"]["puppets"] != 5 or snap["errors"] != ["bus: down"]:
        failed += 1
        print(f"FAIL snapshot body: {snap['counts']} {snap['errors']}")
    return failed


def main():
    failed = (check_classify() + check_projects() + check_sizes()
              + check_journal() + check_usage() + check_snapshot())
    print("web: FAILED" if failed else "web: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
