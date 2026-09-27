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
from _lib import Checks  # noqa: E402
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


def check_classify(c):
    for r, want in CLASSIFY:
        c.expect(f"classify({r.name}: {r.state!r}/{r.alloc_status})", web.classify(r), want)


def check_projects(c):
    got = web.projects(ROWS)
    names = [s["name"] for s in got]
    # По имени проекта, папеты внутри — по имени: порядок на странице стабилен,
    # а puppet_rows_stream отдаёт строки по готовности.
    c.expect("projects order", names, ["?", "mop", "rugent"])
    by = {s["name"]: s for s in got}
    c.expect("puppets order in rugent", [p["name"] for p in by["rugent"]["puppets"]],
             ["pu-rugent-1", "pu-rugent-2"])
    want = {"puppets": 2, "free": 1, "busy": 0, "sick": 0, "silent": 1, "down": 0}
    c.expect("counts(mop)", by["mop"]["counts"], want)
    # Корзина лежит в строке: страница красит по ней, а не разбирает
    # текст состояния второй раз на JS.
    c.check("kind on row", not (by["rugent"]["puppets"][0].get("kind") != "down"),
            by["rugent"]["puppets"][0])
    total = web.counts(ROWS)
    want = {"puppets": 5, "free": 2, "busy": 1, "sick": 0, "silent": 1, "down": 1}
    c.expect("counts(all)", total, want)


def check_sizes(c):
    """Обмер приезжает отдельным поездом и вливается в строки по имени; кого
    не обмерили — прочерк (None), а не ноль и не потеря строки."""
    got = web.with_sizes(ROWS[:2], {"pu-mop-1": 2048})
    kb = {r.name: r.disk_kb for r in got}
    c.expect("with_sizes", kb, {"pu-rugent-2": None, "pu-mop-1": 2048})
    # Исходные строки не трогаем: сборщик держит их между поездами.
    c.check("with_sizes mutated its input", not (ROWS[1].disk_kb is not None))


def check_journal(c):
    """Журнал событий — кольцо: старое вытесняется, порядок от старого к
    новому, вход не мутируется."""
    log = []
    for i in range(5):
        log = web.push(log, {"event": "send", "n": i}, cap=3)
    c.expect("push ring", [e["n"] for e in log], [2, 3, 4])


def check_usage(c):
    """Ось графика — все дни окна, включая пустые: провал виден на месте."""
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
    c.expect("usage_axis", got, want)


def check_snapshot(c):
    """Снимок — один JSON для страницы и /api/pool: всё, что в нём лежит,
    страница читает по имени, поэтому набор ключей закреплён."""
    snap = web.snapshot(rows=ROWS, nodes=[{"name": "mate"}],
                        usage=[], per_puppet=[], per_user=[], journal=[], errors=["bus: down"],
                        at=1_000_000.0)
    # per_user -- расход по людям (#245).
    # creds -- реестр кредитов (#285).
    want = {"at", "projects", "counts", "nodes", "usage", "per_puppet", "per_user",
            "journal", "errors", "creds"}
    c.check("snapshot keys", not (set(snap) != want), sorted(set(snap) ^ want))
    c.check("snapshot body",
            not (snap["counts"]["puppets"] != 5 or snap["errors"] != ["bus: down"]),
            f"{snap['counts']} {snap['errors']}")


def check_by_user_245(c):
    """HYPOTHESIS (#245): оператор видит расход по папетам и по дням, но не
    по людям -- кто сколько тратит, не видно. #244 учит глагол usage агента
    класть рядом со старым полем usage новое by_login:
    {папет: {логин: {дата: {вид: n}}}}, "-" -- неприписанное.
    SOLUTION: чистая свёртка ответов узлов в строки по людям, от самого
    прожорливого; узел со старым агентом (без by_login) -- не ошибка: весь
    расход его папетов -- «-». Сумма по людям равна сумме по папетам.
    STATUS: FIXED — see #245"""
    fn = getattr(web, "user_rows", None)
    if not c.check("#245: web.user_rows exists", fn is not None):
        return

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
    c.expect("#245 user_rows", got, want)
    # Инвариант: по людям -- столько же, сколько по папетам.
    puppets_total = sum(sum(sum(r.values()) for r in rows.values())
                        for a in answers.values() for rows in (a.get("usage") or {}).values())
    c.expect("#245 invariant: users = puppets", sum(r["total"] for r in got), puppets_total)
    snap = web.snapshot(rows=[], nodes=[], usage=[], per_puppet=[], per_user=got,
                        journal=[], errors=[], at=1.0)
    c.expect("#245 snapshot per_user", snap.get("per_user"), got)


def check_sick_in_project_210(c):
    """HYPOTHESIS (#210): счётчик проекта считается по строкам, у которых
    kind уже заменён корзиной, а classify("sick") отвечает busy: больной
    папет попадает в проект занятым, хотя шапка считает верно -- оператор
    видит здоровый проект, когда папет в нём залип.
    SOLUTION: корзина проекта -- тот же classify по виду вердикта, что и
    шапка; в строку страницы корзина кладётся только при сериализации.
    STATUS: FIXED — see #210"""
    rows = [row("pu-mop-1", MOP, State("hung", "not responding")),
            row("pu-mop-2", MOP, State("busy", branch="feat/210"))]
    snap = web.snapshot(rows=rows, nodes=[], usage=[], per_puppet=[], per_user=[], journal=[],
                        errors=[], at=1.0)
    want = {"puppets": 2, "free": 0, "busy": 1, "sick": 1, "silent": 0, "down": 0}
    c.expect("#210 header counts", snap["counts"], want)
    c.expect("#210 project counts", snap["projects"][0]["counts"], want)
    c.expect("#210 bucket on rows", [p["kind"] for p in snap["projects"][0]["puppets"]],
             ["sick", "busy"])


# ── реестр кредитов на странице (#285) ──────────────────────────────────
# Секция «Кредиты» без входа (решение оператора 26.09: LAN доверенная).
# HYPOTHESIS: снимок не несёт реестра, у страницы нет строк кредитов и
# нет разбора тел POST-запросов -- ключ мог бы уехать в снимок или в журнал.
# SOLUTION: web.cred_rows -- строки без секретов со статусом по-русски и
# временем сброса; ключ `creds` в снимке; чистые разборы parse_login_start,
# parse_login_code (разбор добавления ушёл с #294). STATUS: FIXED — see #285
CREDS = [
    {"name": "anton", "profile": "claude", "kind": "login", "owner": "a@x.dev",
     "added_at": 1_000_000 - 7200, "key": "sk-ant-secret",
     "status": {"kind": "active", "resets_at": None, "percent": 46,
                "detail": "5h 26%, weekly 46%", "probed_at": 1_000_000}},
    {"name": "team", "profile": "glm", "kind": "key", "owner": "",
     "added_at": 1_000_000 - 3 * 86400,
     "status": {"kind": "quota_wait", "resets_at": 1_000_000 + 1800, "percent": 100,
                "detail": "5h 100%", "probed_at": 1_000_000}},
    {"name": "old", "profile": "claude", "kind": "token", "owner": "b@x.dev",
     "added_at": 1_000_000 - 60,
     "status": {"kind": "needs_login", "resets_at": None, "percent": None,
                "detail": "HTTP 401", "probed_at": 1_000_000}},
    {"name": "fresh", "profile": "claude", "kind": "login", "owner": "",
     "added_at": 1_000_000, "status": None},
]


def check_creds_285(c):
    rows = web.cred_rows(CREDS, now=1_000_000)
    c.expect("#285 cred names", [r["name"] for r in rows], ["anton", "team", "old", "fresh"])
    c.expect("#285 status words", [r["status"] for r in rows],
             ["активен", "ждёт квоты до " + web.human_time(1_000_000 + 1800),
              "ждёт ручной авторизации: HTTP 401", "не проверялся"])
    c.expect("#285 percent and age", [(r["percent"], r["age"]) for r in rows],
             [(46, "2h"), (100, "3d"), (None, "1m"), (None, "0m")])
    c.check("#285 no secret reaches the page",
            not any(k in r for r in rows for k in ("key", "token", "secret", "detail_raw")),
            rows)
    c.expect("#285 columns", sorted(rows[0]),
             sorted(["name", "profile", "kind", "owner", "status", "resets_at", "percent", "age"]))
    snap = web.snapshot(rows=[], nodes=[], usage=[], per_puppet=[], per_user=[], journal=[],
                        errors=[], at=1.0, creds=rows)
    c.expect("#285 snapshot carries creds", snap.get("creds"), rows)

    c.expect("#285 parse_login_start", web.parse_login_start({"name": "anton"}),
             ({"name": "anton", "mode": "login"}, None))
    c.expect("#285 parse_login_start setup-token", web.parse_login_start({"name": "a", "mode": "setup-token"}),
             ({"name": "a", "mode": "setup-token"}, None))
    c.check("#285 parse_login_start refuses a strange mode",
            web.parse_login_start({"name": "a", "mode": "x"})[0] is None)
    c.expect("#285 parse_login_code", web.parse_login_code({"name": "a", "code": " c#s "}),
             ({"name": "a", "code": "c#s"}, None))
    c.check("#285 parse_login_code refuses an empty code",
            web.parse_login_code({"name": "a", "code": " "})[0] is None)


# ── страница только показывает реестр и авторизует claude в строке (#294) ──
# Решение оператора 27.09: добавление кредитов -- командами `mop cred`, на
# странице -- кнопка «Авторизоваться» в строке кредита claude.
# HYPOTHESIS: у страницы две формы (добавить ключ, войти в claude), маршрут
# /api/creds/add и его разбор parse_cred_add; строка без профиля не даёт
# странице решить, где ставить кнопку.
# SOLUTION: маршрут и разбор добавления убраны, разборы входа остались,
# cred_rows несёт profile. STATUS: FIXED — see #294
def check_row_button_294(c):
    c.check("#294 parse_cred_add is gone", not hasattr(web, "parse_cred_add"))
    from mop.cli.server import web as webcli
    routes = webcli.Handler.ROUTES
    c.check("#294 /api/creds/add is not routed", "/api/creds/add" not in routes, sorted(routes))
    c.expect("#294 login routes stay", sorted(routes),
             ["/api/creds/login/code", "/api/creds/login/start"])
    rows = web.cred_rows(CREDS, now=1_000_000)
    c.expect("#294 rows carry the profile for the button",
             [r["profile"] for r in rows], ["claude", "glm", "claude", "claude"])
    c.expect("#294 parse_login_start still validates",
             web.parse_login_start({"name": "anton"}), ({"name": "anton", "mode": "login"}, None))
    c.check("#294 parse_login_code still refuses an empty code",
            web.parse_login_code({"name": "a", "code": ""})[0] is None)
    page = open(os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                             "web", "index.html"), encoding="utf-8").read()
    c.check("#294 the page has no add form", 'id="cred-add"' not in page and "/api/creds/add" not in page)
    c.check("#294 the page has no login form", 'id="cred-login"' not in page)
    c.check("#294 the page has the row button", "Авторизоваться" in page)


def main():
    c = Checks()
    for fn in (check_classify, check_projects, check_sizes, check_journal, check_usage,
               check_snapshot, check_sick_in_project_210, check_by_user_245,
               check_creds_285, check_row_button_294):
        fn(c)
    return c.report("web")


if __name__ == "__main__":
    sys.exit(main())
