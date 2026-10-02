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
import io
import os
import threading
import json
from io import BytesIO
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.server import identity, web  # noqa: E402
from mop.common.state import PuppetRow, State, silent  # noqa: E402

FREE = State("free", branch="master")


def row(name, origin, state=FREE, alloc="running", node="mate"):
    """Строка puppet_rows: вердикт State, либо None там, где спрашивать некого."""
    return PuppetRow(name=name, node=node, alloc_status=alloc,
                     state=str(state) if state else "-", kind=state and state.kind,
                     owner="-", origin=origin, disk_kb=None)


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
            "journal", "errors", "masters", "masters_every", "load"}
    # masters -- живые мастера по опросу who (#305); masters_every -- период
    # этого опроса (#325): страница его не выдумывает. load -- места пула и
    # полоса аллокаций (#378).
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



# ── страница только показывает реестр и авторизует claude в строке (#294) ──
# Решение оператора 27.09: добавление кредитов -- командами `mop cred`, на
# странице -- кнопка «Авторизоваться» в строке кредита claude.
# HYPOTHESIS: у страницы две формы (добавить ключ, войти в claude), маршрут
# /api/creds/add и его разбор parse_cred_add; строка без профиля не даёт
# странице решить, где ставить кнопку.
# SOLUTION: маршрут и разбор добавления убраны, разборы входа остались,
# cred_rows несёт profile. STATUS: FIXED — see #294
# Вкладка claude открывается сама (#294): порядок «окно до запроса» с #300
# держит Vitest (web/src/components/AuthorizeFlow.test.tsx) -- страница
# стала React-приложением, и старый web/index.html с #301 удалён.




# ── #305: живые мастера на странице ──────────────────────────────────
# HYPOTHESIS: реестра мастеров нет (намеренно, mcp._masters), и дашборд их не
# показывает вовсе; кто жив и кто чем правит, видно только из `agents`.
# SOLUTION: сборщик опрашивает who по проектам, чистая master_rows собирает
# ответы в строки: проект, адрес, логин, сессия, каталог и папеты проекта,
# чья аренда на этом логине; ключ снимка masters. STATUS: FIXED — see #305
def check_masters_305(c):
    import dataclasses
    rows = [dataclasses.replace(row("pu-mop-1", "git@h:g/mop.git"), owner="ermak"),
            dataclasses.replace(row("pu-mop-2", "git@h:g/mop.git"), owner="ivan"),
            dataclasses.replace(row("pu-mop-3", "git@h:g/mop.git"), owner="ermak"),
            dataclasses.replace(row("pu-rugent-1", "git@h:g/rugent.git"), owner="ermak")]
    answers = {
        "mop": [{"master": "ermak.mate-7", "project": "mop", "user": "ermak",
                 "session": "mop-ab", "cwd": "/home/ermak/mop"},
                {"master": "ivan.box-3", "project": "mop", "user": "ivan",
                 "session": None, "cwd": None},
                # тот же мастер ответил дважды (две подписки) -- одна строка:
                # без sessionId (#421) дедуп -- по самому (project, address).
                {"master": "ermak.mate-7", "project": "mop", "user": "ermak",
                 "session": "mop-ab", "cwd": "/home/ermak/mop"}],
        "rugent": [],
    }
    got = web.master_rows(answers, rows)
    c.expect("#305 one row per master, by project, user and address",
             [(m["project"], m["user"], m["master"]) for m in got],
             [("mop", "ermak", "ermak.mate-7"), ("mop", "ivan", "ivan.box-3")])
    c.expect("#305 puppets of the project leased by that login",
             [m["puppets"] for m in got], [["pu-mop-1", "pu-mop-3"], ["pu-mop-2"]])
    c.expect("#305 empty session and directory read as a dash",
             (got[1]["session"], got[1]["cwd"]), ("-", "-"))
    c.expect("#305 a reply without a project falls under the asked one",
             web.master_rows({"x": [{"master": "a.b-1", "user": "a"}]}, [])[0]["project"], "x")
    snap = web.snapshot(rows=[], nodes=[], usage=[], per_puppet=[], per_user=[], journal=[],
                        errors=[], at=1.0, masters=got)
    c.expect("#305 the snapshot carries the masters", len(snap["masters"]), 2)


# ── #297: страница -- собранное React-приложение из web/dist ─────────────
# HYPOTHESIS: сервис отдаёт web/index.html и не умеет статику: у собранного
# приложения скрипт и стили лежат в web/dist/assets/ под именами с хешем, и
# без маршрута /assets/<имя> страница пуста. SOLUTION: PAGE -- web/dist/
# index.html, чистые asset_path (только /assets/<имя> без обхода каталога) и
# content_type (по расширению), cache_control (index -- no-cache, ассеты с
# хешем -- на год). STATUS: FIXED — see #297
def check_dist_297(c):
    root = "/srv/web/dist"
    c.expect("asset_path: a js file under assets", web.asset_path("/assets/index-Ab12.js", root),
             "/srv/web/dist/assets/index-Ab12.js")
    for bad in ("/assets/../index.html", "/assets/", "/assets/a/b.js", "/index.html",
                "/assets/x.js/..", "/assets/.hidden", "/assets/a%2f..%2fb.js"):
        c.check(f"asset_path refuses {bad}", web.asset_path(bad, root) is None, web.asset_path(bad, root))
    c.expect("content_type js", web.content_type("index-Ab12.js"), "text/javascript; charset=utf-8")
    c.expect("content_type css", web.content_type("index-Ab12.css"), "text/css; charset=utf-8")
    c.expect("content_type svg", web.content_type("logo.svg"), "image/svg+xml")
    c.expect("content_type woff2", web.content_type("f.woff2"), "font/woff2")
    c.expect("content_type png", web.content_type("a.png"), "image/png")
    c.expect("content_type map", web.content_type("index.js.map"), "application/json")
    c.expect("content_type unknown", web.content_type("a.exe"), "application/octet-stream")
    c.expect("cache_control index", web.cache_control("/"), "no-cache")
    c.expect("cache_control assets", web.cache_control("/assets/index-Ab12.js"),
             "public, max-age=31536000, immutable")
    from mop.cli.server import web as webcli
    c.check("the service serves web/dist/index.html, not the old page",
            webcli.PAGE.endswith(os.path.join("web", "dist", "index.html")), webcli.PAGE)
    c.check("the committed dist has an index and assets",
            os.path.isfile(webcli.PAGE) and os.path.isdir(os.path.join(os.path.dirname(webcli.PAGE), "assets")))





# ── #319: круг сборщика, запись журнала, схема who (характеристика) ────────
# HYPOTHESIS (DRY после #262): круг сборщика (try → запись под замком →
# _note(вид, None) / except → _note(вид, str(e) or type(e).__name__) →
# _bump) написан пять раз; текст ошибки -- ещё раз в cli/server/web.py; запись
# журнала о кредите там собрана руками, хотя web.journal_entry заполняет
# умолчания; проект строки («?» без origin) -- дважды; схема ответа who и
# MASTERS_WAIT -- у производителя (mcp) и двух читателей; JSON-ответы
# страницы -- шесть раз руками; CRED_FIELDS -- мёртвый.
# SOLUTION: Collector._round, web.error_text, journal_entry в обработчике,
# row_project, domain.MasterAnswer и одна константа ожидания, _json у
# обработчика. Первая половина -- характеристика, снятая ДО переезда: заметки
# кругов, запись журнала, чтение who и байты ответов те же после.
# STATUS: FIXED — see #319
class _Stop(Exception):
    pass


def _stop(*a, **k):
    raise _Stop()


def _one_pass(col, round_):
    """Один проход бесконечного круга: пауза в его конце обрывает цикл."""
    import threading
    col._kick = type("K", (), {"wait": staticmethod(_stop), "is_set": lambda self: False,
                               "clear": lambda self: None, "set": lambda self: None})()
    with patched(web.time, sleep=_stop):
        try:
            round_()
        except _Stop:
            pass


def check_rounds_319(c):
    from mop.common import projects as registry
    boom, empty = RuntimeError("boom"), RuntimeError()

    def raising(e):
        def raise_(*a, **k):
            raise e
        return raise_
    cases = {
        "states": (lambda col: col._states,
                   {"puppet_rows": lambda sizes=False: [], "nodes": lambda: []}, "puppet_rows"),
        "sizes": (lambda col: col._sizes, {"puppet_sizes": lambda rows: {"pu-a-1": 7}}, "puppet_sizes"),
        "masters": (lambda col: col._masters,
                    {"project_ids": lambda reg: ((), ())}, "project_ids"),
    }
    for kind, (method, ok_patch, failing) in cases.items():
        for label, patch, want in (("success", ok_patch, None),
                                   ("failure", {**ok_patch, failing: raising(boom)}, "boom"),
                                   ("failure, empty message", {**ok_patch, failing: raising(empty)},
                                    "RuntimeError")):
            col = web.Collector()
            col.rows = [PuppetRow("pu-a-1", "n1", "running", "free", "free", "-", "claude",
                                  "git@h:g/a.git")]
            col.errors = {kind: "stale"} if want is None else {}
            before = col.version
            with patched(web.puppets, **patch), patched(registry, read=lambda: set(),
                                                         names=lambda *a: []), \
                    patched(web.bus, gather=lambda *a, **k: []):
                _one_pass(col, method(col))
            c.expect(f"#319 round {kind} {label}: the note", col.errors.get(kind), want)
            c.expect(f"#319 round {kind} {label}: one bump", col.version - before, 1)
            if kind == "states":
                c.check(f"#319 round states {label}: at is set", col.at is not None)
    # sizes без строк -- круга нет: ни заметки, ни версии.
    col = web.Collector()
    with patched(web.puppets, puppet_sizes=raising(boom)):
        _one_pass(col, col._sizes)
    c.expect("#319 round sizes without rows: nothing", (col.errors, col.version), ({}, 0))
    # usage -- через gather_usage.
    for label, gu, want in (("success", lambda: ({}, [], []), None), ("failure", raising(boom), "boom")):
        col = web.Collector()
        with patched(web, gather_usage=gu):
            _one_pass(col, col._usage)
        c.expect(f"#319 round usage {label}: note and one bump",
                 (col.errors.get("usage"), col.version), (want, 1))

def _fake_handler(webcli, method, path, body=b"", headers=None, peer=("127.0.0.1", 0)):
    import io
    h = webcli.Handler.__new__(webcli.Handler)
    h.command = method
    h.path = path
    h.headers = {"Content-Length": str(len(body)), **(headers or {})}
    h.rfile, h.wfile, h.sent = io.BytesIO(body), io.BytesIO(), []
    h.client_address = peer
    h.send_response = lambda code, msg=None: h.sent.append(("status", code))
    h.send_header = lambda k, v: h.sent.append((k, v))
    h.end_headers = lambda: None
    getattr(h, "do_" + method)()
    return h.sent, h.wfile.getvalue()


def check_replies_319(c):
    import json
    from mop.cli.server import web as webcli

    class Col:
        def __init__(self):
            self.events, self.refreshed = [], 0

        def event(self, e):
            self.events.append(e)

        def refresh_creds(self):
            self.refreshed += 1

        def current(self):
            return {"название": "пул", "n": 1}

    def head(code, n):
        return [("status", code), ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(n)), ("Cache-Control", "no-store")]
    col = Col()
    login = json.dumps({"name": "anton", "code": "c"}).encode()

def check_who_319(c):
    """Чтение ответа who: страница (master_rows) и инструмент agents (mcp)."""
    from mop.cli.service import mcp
    answer = {"master": "anton.mate-7", "project": "mop", "user": "anton", "session": "s-1",
              "cwd": "/w"}
    bare = {"master": None, "user": None, "session": None, "cwd": None}
    rows = [PuppetRow("pu-mop-1", "n1", "running", "busy", "busy", "anton",
                      "git@h:g/mop.git")]
    c.expect("#319 page: a who answer as a masters row",
             web.master_rows({"mop": [answer, bare]}, rows),
             [{"project": "mop", "master": "-", "addresses": ["-"], "user": "-",
               "session": "-", "cwd": "-", "identified": False, "puppets": []},
              {"project": "mop", "master": "anton.mate-7", "addresses": ["anton.mate-7"],
               "user": "anton", "session": "s-1", "cwd": "/w", "identified": False,
               "puppets": ["pu-mop-1"]}])
    with patched(mcp.bus, gather=lambda *a, **k: [answer, bare]), patched(mcp, MASTER_ID="anton.mate-7"):
        got = mcp._masters()
    c.expect("#319 agents: the masters table from the same answers", got,
             ["", "masters of project:",
              "MASTER (address for send)  USER   SESSION           DIRECTORY",
              "None                       -      -                 -",
              "anton.mate-7               anton  s-1 (this is me)  /w"])


def check_shape_319(c):
    """Одно место на каждое: круг, текст ошибки, запись журнала, проект строки,
    схема who с её ожиданием, JSON-ответ."""
    import ast
    from mop.common import domain
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

    def text(rel):
        return open(os.path.join(root, rel)).read()
    srv, cli, mcp_src = (text("mop/server/web.py"), text("mop/cli/server/web.py"),
                         text("mop/cli/service/mcp.py"))
    c.check("#319 Collector._round: one round for five", hasattr(web.Collector, "_round"))
    c.check("#319 web.error_text: one error text", hasattr(web, "error_text"))
    c.expect("#319 `str(e) or type(e).__name__` is written once",
             (srv + cli).count("type(e).__name__"), 1)
    c.expect("#319 the collector notes only inside _round",
             srv.count("self._note("), 2)
    c.check("#319 the handler builds no journal record by hand",
            '"node": "-"' not in cli and "journal_entry(" in cli)
    c.check("#319 row_project: one project-of-row", hasattr(web, "row_project"))
    c.expect('#319 `if r.origin else "?"` is written once', srv.count('if r.origin else "?"'), 1)
    ma = getattr(domain, "MasterAnswer", None)
    c.check("#319 domain.MasterAnswer: the who schema", ma is not None
            and hasattr(ma, "from_dict") and hasattr(ma, "to_dict"))
    c.check("#319 one wait for who: domain.WHO_WAIT", getattr(domain, "WHO_WAIT", None) == 2)
    for rel, src in (("mop/server/web.py", srv), ("mop/cli/service/mcp.py", mcp_src)):
        c.check(f"#319 {rel}: no MASTERS_WAIT of its own", "MASTERS_WAIT =" not in src)
    tree = ast.parse(cli)
    own = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_json"), None)
    inside = {id(x) for x in ast.walk(own)} if own else set()
    inline = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) and id(n) not in inside
              and getattr(n.func, "attr", None) == "_send"
              and any(isinstance(a, ast.Call) and getattr(a.func, "attr", None) == "dumps"
                      for a in n.args)]
    c.check("#319 Handler._json: one JSON reply", own is not None)
    c.expect("#319 no JSON reply is dumped by hand outside _json", inline, [])
    c.check("#319 CRED_FIELDS is gone: nothing read it", not hasattr(web, "CRED_FIELDS"))


# ── #378: места пула и полоса аллокаций ─────────────────────────────────
# HYPOTHESIS: на странице не было общей картины загрузки пула: в шапке --
# папеты по корзинам, места -- только по узлам в NodesPanel.
# SOLUTION: снимок несёт load -- всего мест, выделено, свободных мест и
# сегменты полосы в порядке busy, free, sick, silent, other, vacant. Считает
# сервер: сумма сегментов -- всего мест. Узел без мест (down, отказ -- None)
# в сумму не входит. Папетов по корзинам больше, чем выделено мест (папет
# попросил меньше spec.MEM, или его узел без ёмкости), -- места раздаются по
# порядку сегментов и кончаются; бейджи корзин остаются честными (counts).
# STATUS: FIXED — see #378
def node(slots, total):
    return {"name": "n", "slots": slots, "slots_total": total}


def segs(load):
    return [(s["kind"], s["slots"]) for s in load["segments"]]


def check_load_378(c):
    fn = getattr(web, "load", None)
    if not c.check("#378 web.load exists", fn is not None):
        return
    busy = row("pu-mop-3", MOP, State("busy"))
    free = row("pu-mop-1", MOP, FREE)
    sick = row("pu-mop-5", MOP, State("hung", "not responding"))
    quiet = row("pu-mop-11", MOP, silent("no responders"))
    gone = row("pu-mop-12", MOP, None, alloc="pending", node="-")
    order = ["busy", "free", "sick", "silent", "other", "vacant"]

    got = fn([], [])
    c.expect("#378 empty pool: nothing anywhere",
             (got["total"], got["allocated"], got["free_slots"], segs(got)),
             (0, 0, 0, [(k, 0) for k in order]))

    got = fn([], [node(3, 3), node(2, 2)])
    c.expect("#378 every slot free: one vacant segment",
             (got["total"], got["allocated"], got["free_slots"], segs(got)),
             (5, 0, 5, [(k, 5 if k == "vacant" else 0) for k in order]))

    # Узел down (None) и узел с отказом мест не держат и в сумму не входят;
    # «не подняты» мест не держат вовсе.
    got = fn([busy, busy, free, gone],
             [node(1, 6), node(None, None), {"name": "x", "error": "boom",
                                             "slots": None, "slots_total": None}])
    c.expect("#378 other: slots held beyond the puppets counted in kinds",
             (got["total"], got["allocated"], got["free_slots"], segs(got)),
             (6, 5, 1, [("busy", 2), ("free", 1), ("sick", 0), ("silent", 0),
                        ("other", 2), ("vacant", 1)]))

    # Бегущих папетов больше, чем выделено мест: места кончаются по порядку
    # сегментов, «прочего» нет, сумма -- всего мест.
    got = fn([busy, free, free, sick, quiet], [node(1, 4)])
    c.expect("#378 more puppets than slots: cut in segment order",
             (got["total"], got["allocated"], got["free_slots"], segs(got)),
             (4, 3, 1, [("busy", 1), ("free", 2), ("sick", 0), ("silent", 0),
                        ("other", 0), ("vacant", 1)]))
    c.expect("#378 the segments add up to every slot",
             sum(n for _, n in segs(got)), got["total"])

    snap = web.snapshot(rows=[busy], nodes=[node(1, 2)], usage=[], per_puppet=[],
                        per_user=[], journal=[], errors=[], at=None)
    c.expect("#378 the snapshot carries the load", snap.get("load"), fn([busy], [node(1, 2)]))


# ── #421: «Мастера» -- уникальные Claude-сессии, не who-адреса ───────────
# HYPOTHESIS: master_rows мерил мастеров who-адресами: у одной живой сессии
# их несколько (инбокс проекта и личный), и каждый адрес становился строкой,
# а папеты приписывались каждому адресу логина. Сессий без sessionId он
# склеивал по (проект, адрес) -- адрес у мёртвой сессии жив, и строки
# плодились; личность сессии в ответе не было вовсе.
# SOLUTION: who-ответ несёт sessionId (модификатор адреса: имя файла
# сессии уникально, имя -- нет); master_rows группирует ответы по
# (project, sessionId), адреса собирает списком -- адресация не теряется;
# без sessionId ответы одной (project, user) не склеиваются. «puppets» --
# папеты логина проекта (не адреса): поле сохранено для старой страницы,
# ownership пояснит UI (#423). Публичный снимок sessionId не раскрывает:
# строка -- лишь указатель «одна сессия», адреса как были.
# STATUS: FIXED — see #421
def check_master_sessions_421(c):
    import dataclasses
    rows = [dataclasses.replace(row("pu-mop-1", "git@h:g/mop.git"), owner="ermak"),
            dataclasses.replace(row("pu-mop-2", "git@h:g/mop.git"), owner="ivan"),
            dataclasses.replace(row("pu-mop-3", "git@h:g/mop.git"), owner="ermak")]
    answers = {
        "mop": [
            # Одна живая сессия, два who-адреса (личный инбокс и общий).
            {"master": "ermak.mate-7", "user": "ermak", "session": "mop-ab",
             "sessionId": "0f1e-2d3c", "cwd": "/h/ermak/mop"},
            {"master": "ermak.mate-7-all", "user": "ermak", "session": "mop-ab",
             "sessionId": "0f1e-2d3c", "cwd": "/h/ermak/mop"},
            # Другая живая сессия того же логина.
            {"master": "ermak.box-9", "user": "ermak", "session": "mop-cd",
             "sessionId": "9b8a-7c6d", "cwd": "/h/ermak/other"},
            # Сессия без sessionId: своя строка, ни с кем не склеена.
            {"master": "ivan.box-3", "user": "ivan", "session": "mop-ef",
             "sessionId": None, "cwd": None},
            {"master": "ivan.box-4", "user": "ivan", "session": "mop-gh",
             "sessionId": None, "cwd": None},
        ],
    }
    got = web.master_rows(answers, rows)
    c.expect("#421 one row per live session, not per who address",
             sorted((m["user"], m["session"]) for m in got),
             [("ermak", "mop-ab"), ("ermak", "mop-cd"), ("ivan", "mop-ef"),
              ("ivan", "mop-gh")])
    ab = next(m for m in got if m["session"] == "mop-ab")
    c.expect("#421 every who address of the session is kept: send still reaches it",
             ab["master"], "ermak.mate-7")
    c.expect("#421 the addresses list carries all of them",
             sorted(ab["addresses"]),
             ["ermak.mate-7", "ermak.mate-7-all"])
    c.expect("#421 the other session carries its own address only",
             next(m for m in got if m["session"] == "mop-cd")["addresses"],
             ["ermak.box-9"])
    # Публичный снимок: сырого sessionId в строках нет.
    c.check("#421 no raw sessionId in the public rows",
            all("sessionId" not in m for m in got), got)
    # #421 follow-up: строка называет, чем она склеена -- сессией или
    # адресом. Без маркера число «сессий» врёт, пока живы старые MCP.
    c.expect("#421 a sessionId row is identified",
             [m["identified"] for m in got],
             [True, True, False, False])
    # «puppets» -- папеты логина в проекте; для старой страницы поле на месте
    # и не множится по адресам (#423 пояснит ownership).
    c.expect("#421 puppets follow the login, once per session row",
             [m["puppets"] for m in got],
             [["pu-mop-1", "pu-mop-3"], ["pu-mop-1", "pu-mop-3"],
              ["pu-mop-2"], ["pu-mop-2"]])
    # Дубль ответа той же сессии -- по-прежнему одна строка.
    twice = web.master_rows({"mop": answers["mop"] + [answers["mop"][0]]}, rows)
    c.expect("#421 a duplicate who answer adds no row", len(twice), 4)
    # Legacy (#421, ревью): повтор ТОГО ЖЕ who-адреса без sessionId -- один
    # ответивший endpoint, одна строка; разные адреса -- разные строки.
    legacy = [{"master": "ermak.mate-7", "user": "ermak", "session": "mop-ab",
               "sessionId": None, "cwd": "/h/ermak/mop"}]
    c.expect("#421 the same legacy address twice: one row",
             len(web.master_rows({"mop": legacy + legacy}, rows)), 1)
    c.expect("#421 different legacy addresses: two rows",
             len(web.master_rows({"mop": legacy + [{**legacy[0], "master": "ermak.box-9"}]},
                                 rows)), 2)


def check_who_answer_421(c):
    from mop.common.domain import MasterAnswer
    d = {"master": "a.b-1", "project": "mop", "user": "a", "session": "s",
         "sessionId": "0f1e-2d3c", "cwd": "/w"}
    a = MasterAnswer.from_dict(d)
    c.expect("#421 MasterAnswer carries the session identity", a.session_id, "0f1e-2d3c")
    c.expect("#421 to_dict keeps it on the wire",
             MasterAnswer.from_dict(a.to_dict()).session_id, "0f1e-2d3c")
    c.expect("#421 an old answer without it reads as None",
             MasterAnswer.from_dict({"master": "a.b-1"}).session_id, None)

# ── #415: вход дашборда -- серверная HTTP-сессия file/LDAP ───────────────
# HYPOTHESIS: у страницы входа нет вовсе: /api/pool и /events открыты, и
#серверный гейт появиться не может -- некому спросить «кто ты».
# SOLUTION: GET /api/session, POST /api/login, POST /api/logout в
# mop/server/websession.py: провайдер identity.server_provider (file/LDAP,
# одна на callout и identity), серверная сессия с TTL и отзывом, cookie
# Secure HttpOnly SameSite=Strict Path=/, rate limit входа, Origin строго
# публичный https origin, admin-гейт fail closed снаружи. /api/pool и
# /events не тронуты.
# STATUS: FIXED — see #415
GUEST = b'{"login": null, "role": null}'


class FakeProvider:
    """Провайдер по образцу identity: authenticate -> Identity | Refused."""

    def __init__(self, refusals=()):
        self.refusals = list(refusals)
        self.asked = []

    def authenticate(self, login, password):
        self.asked.append((login, password))
        if self.refusals:
            raise identity.Refused(self.refusals.pop(0))
        if password != "s3cret":
            raise identity.Refused("wrong password")
        return identity.Identity(login, "admin" if login == "anton" else "user", ())

    def lookup(self, login):
        return identity.Identity(login, "admin" if login == "anton" else "user", ())


def _login_body(**kw):
    import json as _json
    return _json.dumps(kw).encode()


def check_session_store_415(c):
    from mop.server import websession
    store = websession.SessionStore(now=lambda: 1000.0)
    store2 = websession.SessionStore(now=lambda: 1000.0)
    # Речек роли admin идёт провайдером; в проверке он -- подмена.
    with patched(websession, provider=lambda: FakeProvider()):
        tok = store.open(identity.Identity("anton", "admin", ()))
        c.check("#415 open: a token of session strength", len(tok) >= 32, len(tok))
        c.expect("#415 a fresh session reads back", store.get(tok, 1000.0),
                 ("anton", "admin"))
        c.expect("#415 revoked by logout", (store.close(tok), store.get(tok, 1000.0)),
                 (True, None))
        c.expect("#415 TTL: expired reads as guest",
                 store.get(store2.open(identity.Identity("anton", "admin", ())),
                           1000.0 + websession.TTL + 1), None)
        c.expect("#415 close of a stranger: no exception", store.close("x" * 40), False)
        other = store.open(identity.Identity("olga", "user", ()))
        c.check("#415 tokens are not alike", tok != other, (tok, other))



def check_login_api_415(c):
    import json
    import re
    import os as _os
    from mop.cli.server import web as webcli
    from mop.server import websession
    good = {"Origin": "https://10.0.0.1", "Content-Type": "application/json"}
    store = websession.SessionStore(now=lambda: 1000.0)
    provider = FakeProvider()
    sent, out = [], b""
    with patched(webcli.config, get=lambda name, default=None: {"MOP_SERVER_LAN": "10.0.0.1",
                                                                "MOP_HTTPS_PORT": "443"}.get(name, default)), \
            patched(webcli, SESSIONS=store), patched(websession, provider=lambda: provider):
        sent, out = _fake_handler(webcli, "GET", "/api/session")
        c.expect("#415 GET /api/session: the guest", out, GUEST)
        sent, out = _fake_handler(webcli, "POST", "/api/login", _login_body(login="anton", password="s3cret"), good)
        c.expect("#415 POST /api/login: the who and role", out, b'{"login": "anton", "role": "admin"}')
        cookie = dict((k, v) for k, v in sent if k == "Set-Cookie")
        c.check(f"#415 the cookie: Secure HttpOnly SameSite=Strict Path=/: {cookie}",
                "Secure" in cookie.get("Set-Cookie", "") and "HttpOnly" in cookie.get("Set-Cookie", "")
                and "SameSite=Strict" in cookie.get("Set-Cookie", "") and "Path=/" in cookie.get("Set-Cookie", ""))
        token = cookie["Set-Cookie"].split("=", 1)[1].split(";", 1)[0]
        sent, out = _fake_handler(webcli, "GET", "/api/session", headers={"Cookie": f"mop_session={token}"})
        c.expect("#415 GET /api/session: the logged-in", out,
                 b'{"login": "anton", "role": "admin"}')
        sent, out = _fake_handler(webcli, "POST", "/api/logout", b"",
                                  {**good, "Cookie": f"mop_session={token}"})
        c.expect("#415 POST /api/logout: the guest again", out, GUEST)
        sent, out = _fake_handler(webcli, "GET", "/api/session", headers={"Cookie": f"mop_session={token}"})
        c.expect("#415 after logout the cookie no longer logs in", out, GUEST)

        # Отказ: одна фраза без причины, никакого перебора логинов.
        sent, out = _fake_handler(webcli, "POST", "/api/login",
                                  _login_body(login="anton", password="nope"), good)
        c.expect("#415 a wrong password: 401, one phrase", (sent[0][1], out),
                 (401, b'{"error": "invalid login or password"}'))
        c.expect("#415 the wrong password still cost the provider one ask",
                 provider.asked[-1], ("anton", "nope"))
        sent, out = _fake_handler(webcli, "POST", "/api/login", _login_body(login="x"), good)
        c.expect("#415 a broken body: 400, not a trace", (sent[0][1], json.loads(out)["error"][:11]),
                 (400, "bad request"))
        # CSRF/Origin: оба POST -- только JSON с нашим https origin.
        for what, hdrs in (("no Origin", {}),
                           ("a foreign Origin", {"Origin": "https://evil.example"}),
                           ("a form post", {"Origin": "https://10.0.0.1",
                                            "Content-Type": "application/x-www-form-urlencoded"})):
            sent, out = _fake_handler(webcli, "POST", "/api/login",
                                      _login_body(login="anton", password="s3cret"),
                                      hdrs or None)
            c.expect(f"#415 login refused: {what}", (sent[0][1], out),
                     (403, b'{"error": "origin refused"}'))
        c.expect("#415 origin refusals never reach the provider",
                 provider.asked[-1], ("anton", "nope"))
        # Rate limit: пятый и дальше -- 429, окно одно.
        for i in range(4):
            _fake_handler(webcli, "POST", "/api/login",
                          _login_body(login="anton", password="nope"), good)
        sent, out = _fake_handler(webcli, "POST", "/api/login",
                                  _login_body(login="anton", password="s3cret"), good)
        c.expect("#415 after a burst of failures login is 429", (sent[0][1], out),
                 (429, b'{"error": "too many attempts, try later"}'))
    # /api/pool остался открытым; /events -- тот же разбор пути, без входа
    # (гонять SSE в проверке -- вечно ждать следующего кадра).
    sent, out = _fake_handler(webcli, "GET", "/api/pool")
    c.expect("#415 /api/pool stays public", sent[0][1], 200)
    src = open(_os.path.join(_os.path.dirname(webcli.__file__), "web.py")).read()
    gate = re.search(r"do_GET.*?/api/session.*?/api/pool", src, re.S)
    c.check("#415 /events keeps its branch with no session gate",
            "if path == \"/events\":" in src and gate is not None
            and src.index("/events") > src.index("/api/pool"))


# Источник адреса для лимита (#415): через nginx все -- 127.0.0.1, и ключ
# по сокету запирал бы вход всей конторе после пяти неудач одного.
# ── #415: источник адреса лимита ─────────────────────────────────────────
# HYPOTHESIS: ключ лимита по сокету за nginx -- 127.0.0.1 у всех (5 неудач
# одного запирали вход всей конторе), а X-Real-IP из доверия -- подделка:
# nginx его не ставит и не срезает, и клиентский X-Real-IP доходил до web.
# SOLUTION: websession.client_addr -- последний элемент X-Forwarded-For
# (его ставит сам nginx своим $remote_addr), только при peer loopback и
# строго валидированный; иначе -- адрес сокета.
# STATUS: FIXED — see #415
def check_address_source_415(c):
    from mop.cli.server import web as webcli
    from mop.server import websession, identity

    c.expect("#415 the loopback proxy: the LAST XFF element is the key",
             websession.client_addr({"X-Forwarded-For": "spoofed, 10.1.2.3"},
                                    ("127.0.0.1", 0)), "10.1.2.3")
    c.expect("#415 X-Real-IP is not trusted at all",
             websession.client_addr({"X-Real-IP": "10.9.9.9"}, ("127.0.0.1", 0)),
             "127.0.0.1")
    c.expect("#415 garbage in XFF: the socket peer",
             websession.client_addr({"X-Forwarded-For": "not-an-ip"}, ("127.0.0.1", 0)),
             "127.0.0.1")
    c.expect("#415 not from the loopback: headers ignored",
             websession.client_addr({"X-Forwarded-For": "1.2.3.4", "X-Real-IP": "5.6.7.8"},
                                    ("10.9.9.9", 0)), "10.9.9.9")
    # Гейт касается эндпоинта (#416), а не входа: admin входит и мимо петли.
    good = {"Origin": "https://10.0.0.1", "Content-Type": "application/json"}
    store = websession.SessionStore(now=lambda: 1000.0)
    with patched(webcli.config, get=lambda name, default=None: {"MOP_SERVER_LAN": "10.0.0.1",
                                                                "MOP_HTTPS_PORT": "443"}.get(name, default)), \
            patched(webcli, SESSIONS=store, RATE=websession.RateLimiter(now=lambda: 1000.0)), \
            patched(websession, provider=lambda: FakeProvider()):
        sent, out = _fake_handler(webcli, "POST", "/api/login",
                                  _login_body(login="anton", password="s3cret"), good,
                                  peer=("10.5.5.5", 0))
        c.expect("#415 an admin logs in from any address: the gate is the endpoint's",
                 (sent[0][1], out), (200, b'{"login": "anton", "role": "admin"}'))
        # Разные клиенты за одним nginx -- разные вёдра: чужие неудачи не
        # запирают вход, и клиентский префикс XFF их не смешивает.
        a = {"Origin": "https://10.0.0.1", "Content-Type": "application/json",
             "X-Forwarded-For": "spoofed, 10.1.1.1"}
        for _ in range(5):
            _fake_handler(webcli, "POST", "/api/login",
                          _login_body(login="anton", password="nope"), a)
        sent, out = _fake_handler(webcli, "POST", "/api/login",
                                  _login_body(login="anton", password="nope"), a)
        c.expect("#415 the flooded address is locked", sent[0][1], 429)
        sent, out = _fake_handler(webcli, "POST", "/api/login",
                                  _login_body(login="anton", password="s3cret"),
                                  {**good, "X-Forwarded-For": "spoofed, 10.2.2.2"})
        c.expect("#415 the neighbour behind the same nginx still logs in",
                 (sent[0][1], out), (200, b'{"login": "anton", "role": "admin"}'))


# ── #415: удалённый пользователь ────────────────────────────────────────
# HYPOTHESIS: SessionStore.get перечитывал провайдера только у admin, и
# удалённый из file/LDAP user оставался вошедшим весь TTL вопреки отзыву.
# SOLUTION: личность перечитывается у каждой сессии: lookup None -- гость
# сразу, любая роль.
# STATUS: FIXED — see #415
def check_deleted_user_415(c):
    from mop.server import identity, websession

    class Gone:
        def lookup(self, login):
            return None

    store = websession.SessionStore(now=lambda: 1000.0)
    tok = store.open(identity.Identity("olga", "user", ()))
    with patched(websession, provider=lambda: Gone()):
        c.expect("#415 a user deleted from the provider loses the session",
                 store.get(tok, 1000.0), None)

# ── #424: админский журнал ──────────────────────────────────────────────
# HYPOTHESIS: журнал событий (кто/кому/что посылал, узлы, проекты) уезжал
# публичным снимком /api/pool и SSE /events любому в LAN, а события с шины
# фильтровались до пяти полей, теряя остальное. SOLUTION: полный журнал --
# отдельное кольцо сборщика и отдельный GET /api/journal под актуальной
# admin-ролью (401 гостю, 403 вошедшему user, fail closed при ошибке
# провайдера); снимок и SSE несут journal: [] на переход. Запись хранит все
# поля события, кроме credential-полей, исходный at с фолбеком на время
# получения и отдельный received_at; событие больше ENTRY_MAX байт
# сериализации заменяется маркером.
# STATUS: FIXED — see #424
import base64 as _b64


def _journal_collector():
    """Сборщик без потоков: только журнал и снимок."""
    col = web.Collector.__new__(web.Collector)
    col.journal, col.admin_journal, col.at = [], [], None
    col.rows, col.nodes, col.usage = [], [], []
    col.per_puppet, col.per_user, col.masters = [], [], []
    col.sizes = {}
    col._cond = threading.Condition()
    col.version, col.errors = 0, {}
    col._kick = threading.Event()
    return col


def check_journal_api_424(c):
    import json
    from mop.cli.server import web as webcli
    from mop.server import websession
    store = websession.SessionStore(now=lambda: 1000.0)
    provider = FakeProvider()
    good = {"Origin": "https://10.0.0.1"}
    admin = store.open(identity.Identity("anton", "admin", ()))
    user = store.open(identity.Identity("olga", "user", ()))

    with patched(webcli.config, get=lambda name, default=None: {"MOP_SERVER_LAN": "10.0.0.1",
                                                                "MOP_HTTPS_PORT": "443"}.get(name, default)), \
            patched(webcli, SESSIONS=store, COLLECTOR=_journal_collector()), \
            patched(websession, provider=lambda: provider):
        col = webcli.COLLECTOR
        col.event({"event": "send", "node": "n1", "name": "pu-x-1",
                   "project": "x", "text": "hi", "at": 100.5, "extra": "kept"})
        # guest: 401, no-store
        sent, out = _fake_handler(webcli, "GET", "/api/journal")
        c.expect("#424 guest: 401 login required", (sent[0][1], out),
                 (401, b'{"error": "login required"}'))
        c.check("#424 refusals carry no-store",
                any(k == "Cache-Control" and v == "no-store" for k, v in sent))
        # user: 403
        sent, out = _fake_handler(webcli, "GET", "/api/journal",
                                  headers={"Cookie": f"mop_session={user}"})
        c.expect("#424 logged-in user: 403 admin only", (sent[0][1], out),
                 (403, b'{"error": "admin only"}'))
        # admin: 200, все поля, обе метки времени
        sent, out = _fake_handler(webcli, "GET", "/api/journal",
                                  headers={"Cookie": f"mop_session={admin}"})
        if not c.check("#424 admin: the route answers JSON",
                       out.startswith(b'{"journal"'), out[:60]):
            return
        got = json.loads(out)["journal"]
        c.check("#424 admin: 200 with the entries",
                sent[0][1] == 200 and len(got) == 1, (sent[0][1], got))
        c.expect("#424 the entry keeps every ordinary field",
                 {k: got[0].get(k) for k in ("event", "node", "name", "project", "text", "extra")},
                 {"event": "send", "node": "n1", "name": "pu-x-1", "project": "x",
                  "text": "hi", "extra": "kept"})
        c.expect("#424 the original at survives", got[0].get("at"), 100.5)
        c.check("#424 received_at is separate and sane",
                isinstance(got[0].get("received_at"), (int, float))
                and got[0]["received_at"] >= 100.5, got[0])

        # Публичность: снимок и SSE без записей.
        snap = col.current()
        c.expect("#424 /api/pool carries no journal entries", snap.get("journal"), [])
        sent, out = _fake_handler(webcli, "GET", "/api/pool")
        c.check("#424 /api/pool 200 and no entry in the wire",
                sent[0][1] == 200 and b"pu-x-1" not in out, out[:120])
        # Настоящий первый кадр /events: тестовый Handler с wfile, который
        # рвёт соединение после первой записи -- до пинга.
        class _Pipe(BytesIO):
            def write(self, data):
                out = super().write(data)
                raise BrokenPipeError()
        class _Handler(webcli.Handler.__class__):
            pass
        h = webcli.Handler.__new__(webcli.Handler)
        h.path = "/events"
        h.headers = {}
        h.wfile = _Pipe()
        h.rfile = BytesIO()
        h.client_address = ("127.0.0.1", 0)
        h.send_response = lambda code, msg=None: None
        h.send_header = lambda k, v: None
        h.end_headers = lambda: None
        try:
            h.do_GET()
        except BrokenPipeError:
            pass
        frame = h.wfile.getvalue()
        c.check("#424 the first /events frame carries no journal entry",
                frame.startswith(b"event: snapshot\ndata: ")
                and b"pu-x-1" not in frame and b"\"journal\": []" in frame,
                frame[:120])
        src = open(os.path.join(os.path.dirname(webcli.__file__), "web.py")).read()
        c.check("#424 /events still streams the snapshot, no journal of its own",
                "if path == \"/events\":" in src and "journal" not in src.split('def stream')[1][:600])

        # Смена роли admin -> user: та же cookie, уже не админ.
        class _NowUser:
            def lookup(self, login):
                return identity.Identity(login, "user", ()) if login == "anton" else None
        with patched(websession, provider=lambda: _NowUser()):
            sent, out = _fake_handler(webcli, "GET", "/api/journal",
                                      headers={"Cookie": f"mop_session={admin}"})
            c.expect("#424 a demoted admin gets 403 on the same cookie",
                     (sent[0][1], out), (403, b'{"error": "admin only"}'))

        # Отзыв сессии и ошибка провайдера: fail closed, не 500.
        store.close(admin)
        sent, out = _fake_handler(webcli, "GET", "/api/journal",
                                  headers={"Cookie": f"mop_session={admin}"})
        c.expect("#424 a revoked session is a guest again", (sent[0][1], out),
                 (401, b'{"error": "login required"}'))

        class _Broken:
            def lookup(self, login):
                raise RuntimeError("provider down")
        admin2 = store.open(identity.Identity("anton", "admin", ()))
        with patched(websession, provider=lambda: _Broken()):
            sent, out = _fake_handler(webcli, "GET", "/api/journal",
                                      headers={"Cookie": f"mop_session={admin2}"})
            c.expect("#424 a provider failure fails closed", (sent[0][1], out),
                     (401, b'{"error": "login required"}'))


def check_journal_entry_424(c):
    col = _journal_collector()
    # at отсутствует/невалиден (bool, NaN, inf, строка): валидное время
    # получения, не null; received_at всегда отдельный.
    cases = [
        ({"event": "send", "text": "a"}, None),
        ({"event": "send", "text": "b", "at": True}, None),
        ({"event": "send", "text": "c", "at": float("nan")}, None),
        ({"event": "send", "text": "d", "at": float("inf")}, None),
        ({"event": "send", "text": "e", "at": "100.5"}, None),
        ({"event": "send", "text": "f", "at": 200.25}, 200.25),
    ]
    for msg, want_at in cases:
        col.event(msg)
    got = col.admin_journal
    for i, (msg, want_at) in enumerate(cases):
        if not c.check(f"#424 the entry carries both timestamps ({msg.get('text')})",
                       "at" in got[i] and "received_at" in got[i], got[i]):
            continue
        at, received = got[i]["at"], got[i]["received_at"]
        c.check(f"#424 at fallback for {msg.get('text')}: valid receipt time",
                isinstance(at, (int, float)) and not isinstance(at, bool)
                and at == at and -1e12 < at < 1e12, (msg.get("at"), at))
        if want_at is not None:
            c.expect(f"#424 a valid original at is kept ({msg.get('text')})", at, want_at)
        c.check(f"#424 received_at is separate ({msg.get('text')})",
                isinstance(received, (int, float)), received)
    # Будущая исходная метка (рассинхрон часов) сохраняется как есть,
    # независимо от received_at.
    colf = _journal_collector()
    colf.event({"event": "send", "text": "future", "at": 4102444800.0})
    c.expect("#424 a future original at survives untouched",
             (colf.admin_journal[0]["at"], colf.admin_journal[0]["received_at"] < 4102444800.0),
             (4102444800.0, True))

    # Credential-поля не хранятся и не отдаются, на любом регистре и вложенно.
    col2 = _journal_collector()
    col2.event({"event": "send", "text": "x", "management_key": "MK",
                "Authorization": "Bearer y", "user_password": "p",
                "nested": {"token": "t", "keep": 1}, "SECRET": "s"})
    flat = json.dumps(col2.admin_journal)
    for secret in ("MK", "Bearer y", '"p"', '"t"', '"s"'):
        c.check(f"#424 credential values never stored ({secret})", secret not in flat, flat)
    c.check("#424 non-credential nested fields survive",
            col2.admin_journal
            and col2.admin_journal[0].get("nested", {}).get("keep") == 1,
            col2.admin_journal[0] if col2.admin_journal else None)

    # Oversized: событие целиком больше ENTRY_MAX байт сериализации --
    # заменяется ограниченным маркером, не хранится как есть.
    col3 = _journal_collector()
    col3.event({"event": "send", "text": "small", "at": 1.0})
    huge = "z" * (web.ENTRY_MAX + 1024)
    col3.event({"event": "send", "text": "big", "blob": huge, "at": 2.0})
    if not c.check("#424 two entries after an oversized one",
                   len(col3.admin_journal) == 2, len(col3.admin_journal)):
        return
    c.check("#424 an oversized event becomes a bounded marker",
            "blob" not in col3.admin_journal[1]
            and len(json.dumps(col3.admin_journal[1]).encode()) <= web.ENTRY_MAX,
            len(json.dumps(col3.admin_journal[1]).encode()))
    c.check("#424 the marker names the event",
            col3.admin_journal[1].get("event") == "send"
            and isinstance(col3.admin_journal[1].get("text"), str), col3.admin_journal[1])
    # ── #424, доработка: ключи и bearer в text ──────────────────────────
    # HYPOTHESIS: список CREDENTIAL_KEYS ловит поля, но не ловит ни ключей
    # api_key/access_key/credentials/passwd, ни типового секрета ВНУТРИ
    # text: «Authorization: Bearer <фиктивный>» сохранялся в admin-журнал.
    # SOLUTION: список дополнен массовыми именами ключей; из text события
    # убирается bearer-значение (типовая форма), остальной текст остаётся --
    # без претензии распознать произвольный секрет в свободном тексте.
    # STATUS: FIXED — see #424
    col4 = _journal_collector()
    col4.event({"event": "send", "text": "x", "api_key": "AK", "access_key": "AK2",
                "credentials": "c", "passwd": "p", "api_token": "t"})
    flat4 = json.dumps(col4.admin_journal)
    for secret in ('"AK"', '"AK2"', '"c"', '"p"', '"t"'):
        c.check(f"#424 key {secret} never stored", secret not in flat4, flat4)

    # Ключ=значение и флаги команд в text (#424, follow-up): источник --
    # команды агента (type=command), типовые формы теряют значение, словарь
    # ключей -- признанный список, произвольная строка секретом не считается.
    col6 = _journal_collector()
    col6.event({"event": "type", "text":
                "ran: mop login --token tok-FAKE1 --password=hunter2 now"})
    flat6 = json.dumps(col6.admin_journal)
    c.check("#424 --token VALUE from a command is redacted", "tok-FAKE1" not in flat6, flat6)
    c.check("#424 --password=VALUE from a command is redacted", "hunter2" not in flat6, flat6)
    c.check("#424 the command around the secrets survives",
            "mop login" in flat6 and "now" in flat6, flat6)
    col7 = _journal_collector()
    col7.event({"event": "type", "text":
                "set api_key=sk-FAKE2 and ACCESS_KEY: ak-FAKE3, done"})
    flat7 = json.dumps(col7.admin_journal)
    c.check("#424 key=value in text is redacted", "sk-FAKE2" not in flat7, flat7)
    c.check("#424 KEY: value in text is redacted (case-insensitive key)",
            "ak-FAKE3" not in flat7, flat7)
    c.check("#424 the context around survives", "set" in flat7 and "done" in flat7, flat7)
    # Не-ключ не трогается: значение после постороннего слова остаётся --
    # редакция не претендует на распознавание произвольных строк.
    col8 = _journal_collector()
    col8.event({"event": "type", "text": "see chapter=42 of the spec"})
    c.expect("#424 a non-credential key keeps its value",
             col8.admin_journal[0].get("text"), "see chapter=42 of the spec")
    # Oversize-маркер, короткое имя (#424, follow-up 2): имя события
    # укладывается в 64 байта и несёт bearer -- маркер обязан отдать его
    # отредактированным, не сырым. То же -- другие текстовые поля.
    colA = _journal_collector()
    colA.event({"event": "Bearer sk-SHORT1", "text": "big",
                "blob": "z" * (web.ENTRY_MAX + 1024)})
    flatA = json.dumps(colA.admin_journal)
    c.check("#424 the oversize marker redacts a short bearer event name",
            "sk-SHORT1" not in flatA, flatA[:200])
    colB = _journal_collector()
    colB.event({"event": "cmd", "note": "Bearer sk-SHORT2",
                "blob": "z" * (web.ENTRY_MAX + 1024)})
    flatB = json.dumps(colB.admin_journal)
    c.check("#424 the oversize marker redacts other text fields of the event",
            "sk-SHORT2" not in flatB, flatB[:200])

    # Oversize-маркер: длинное имя события с bearer внутри не возвращается
    # сырым -- маркер ограничен и заменяет имя.
    col9 = _journal_collector()
    col9.event({"event": "x" * 40 + " Bearer " + "s" * 40, "text": "big",
                "blob": "z" * (web.ENTRY_MAX + 1024)})
    flat9 = json.dumps(col9.admin_journal)
    c.check("#424 the oversize marker never returns a raw bearer name",
            "Bearer s" not in flat9, flat9[:200])

    col5 = _journal_collector()
    col5.event({"event": "type", "text":
                "switched model: Authorization: Bearer sk-ant-FAKE123, please re-check"})
    flat5 = json.dumps(col5.admin_journal)
    c.check("#424 a bearer value in text is redacted",
            "sk-ant-FAKE123" not in flat5, flat5)
    c.check("#424 the text around the secret survives",
            "switched model" in flat5 and "please re-check" in flat5, flat5)

    # Длинное имя события само раздувает запись: маркер обязан быть
    # ограничен целиком, без сырого большого поля -- в нём может быть секрет.
    col5 = _journal_collector()
    col5.event({"event": "send-" + "z" * (web.ENTRY_MAX + 512),
                "text": "short", "at": 3.0})
    marker = col5.admin_journal[0]
    c.check("#424 an oversized event name yields a bounded marker",
            len(json.dumps(marker).encode()) <= web.ENTRY_MAX
            and marker.get("event") != "send-" + "z" * (web.ENTRY_MAX + 512),
            len(json.dumps(marker).encode()))
    # Кольцо 100: старое вытесняется, больше не отдаётся.
    col4 = _journal_collector()
    for i in range(120):
        col4.event({"event": "send", "text": f"e{i}", "at": float(i)})
    c.check("#424 the ring keeps the last 100",
            len(col4.admin_journal) == 100
            and col4.admin_journal[0]["text"] == "e20"
            and col4.admin_journal[-1]["text"] == "e119",
            len(col4.admin_journal))



# ── #415: границы входных данных входа и отказ провайдера на /api/session ─
# HYPOTHESIS: api_login читает тело как rfile.read(int(Content-Length)) без
# потолка и без таймаута: заявленный гигабайт паркует поток навсегда (один
# заголовок -- один вечный поток), а реальное тело копится в память целиком.
# do_GET у /api/session зовёт _who() без защиты: провал провайдера (LDAP
# лежит) уносит исключение в BaseHTTPRequestHandler -- 500 и traceback в
# журнал сервиса вместо контролируемого ответа.
# SOLUTION: тело входа ограничено (BODY_MAX байт) и читается с таймаутом
# (Handler.timeout): oversized -- 413 одной фразой, недосланное тело --
# 400 по истечении таймаута, а не вечный поток. /api/session при любой
# ошибке провайдера отвечает гостем: fail closed -- тот же контракт, что у
# /api/journal (#424).
# STATUS: FIXED — see #415
def check_login_bounds_415(c):
    import socket
    import threading
    import time
    from http.server import ThreadingHTTPServer
    from unittest import mock
    from mop.cli.server import web as webcli
    from mop.server import websession
    good = {"Origin": "https://10.0.0.1", "Content-Type": "application/json"}
    settings = {"MOP_SERVER_LAN": "10.0.0.1", "MOP_HTTPS_PORT": "443"}

    # (a) oversize: тело больше BODY_MAX -- 413, до чтения и до лимита попыток.
    # Провайдер -- шпион: oversized не должен дойти до authenticate.
    spy = FakeProvider()
    body_max = getattr(webcli, "BODY_MAX", 1024 * 1024)
    body = b'{"login": "anton", "password": "s3cret", "pad": "' + b"x" * (body_max + 1) + b'"}'
    with mock.patch.object(webcli.config, "get",
                           lambda n, d=None: settings.get(n, d)), \
            mock.patch.object(webcli, "SESSIONS", websession.SessionStore(now=lambda: 1000.0)), \
            mock.patch.object(webcli, "RATE", websession.RateLimiter(now=lambda: 1000.0)), \
            mock.patch.object(websession, "provider", lambda: spy):
        sent, out = _fake_handler(webcli, "POST", "/api/login", body,
                                  {**good, "Content-Length": str(len(body))})
        c.expect("#415 an oversized login body: 413, one phrase",
                 (sent[0][1], out), (413, b'{"error": "request too large"}'))
        c.expect("#415 the oversized body never reaches the provider",
                 spy.asked, [])

    # (b) недосланное тело: живой сокет, заявленный Content-Length больше
    # посланного. Поток обязан закончиться таймаутом (Handler.timeout),
    # ответ -- 400, соединение закрывается, вечного потока не остаётся.
    class Up:
        def lookup(self, login):
            return identity.Identity(login, "admin", ())

    with mock.patch.object(webcli.Handler, "timeout", 2):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), webcli.Handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(webcli.config, "get",
                                   lambda n, d=None: settings.get(n, d)), \
                    mock.patch.object(webcli, "RATE", websession.RateLimiter(now=lambda: 1000.0)), \
                    mock.patch.object(websession, "provider", lambda: Up()):
                # Заявленный размер -- правдоподобный и под потолком: отказ
                # должен прийти не по размеру, а по таймауту недосланного тела.
                claim = webcli.BODY_MAX // 2
                t0 = time.monotonic()
                s = socket.create_connection(("127.0.0.1", port), timeout=30)
                s.sendall(b"POST /api/login HTTP/1.1\r\nHost: x\r\n"
                          b"Origin: https://10.0.0.1\r\nContent-Type: application/json\r\n"
                          + f"Content-Length: {claim}\r\n\r\n".encode())
                s.sendall(_login_body(login="anton", password="s3cret")[:10])
                s.settimeout(30)
                data = b""
                while b"\r\n" not in data:
                    got = s.recv(256)
                    if not got:
                        break
                    data += got
                took = time.monotonic() - t0
                s.close()
        finally:
            srv.shutdown()
            srv.server_close()
        head = data.split(b"\r\n")[0]
        c.check("#415 an undelivered body ends in a bounded time, not a parked thread",
                head.startswith(b"HTTP/1.0 400 ") or head.startswith(b"HTTP/1.1 400 "),
                (head, f"{took:.1f}s"))
        c.check("#415 the timeout is Handler.timeout, not forever",
                took < 10, (head, f"{took:.1f}s"))

    # (c) /api/session при провале провайдера: контролируемый отказ, не 500.
    class Broken:
        def lookup(self, login):
            raise RuntimeError("provider down")

    store = websession.SessionStore(now=lambda: 1000.0)
    tok = store.open(identity.Identity("anton", "admin", ()))
    with mock.patch.object(webcli.config, "get",
                           lambda n, d=None: settings.get(n, d)), \
            mock.patch.object(webcli, "SESSIONS", store), \
            mock.patch.object(websession, "provider", lambda: Broken()):
        sent, out = _fake_handler(webcli, "GET", "/api/session",
                                  headers={"Cookie": f"mop_session={tok}"})
        c.check("#415 a failed provider on /api/session answers controlled, not a raise",
                sent[0][1] == 200 and json.loads(out)["login"] is None,
                (sent[0][1], out))
        c.expect("#415 the failure says why, one line",
                 json.loads(out).get("error", "").startswith("provider unavailable"),
                 True)
# ── #417: write-guard гейтвея управления прокси ─────────────────────────
# HYPOTHESIS: полный PUT конфига из сток-панели (Source-режим) удаляет блок
# management и убивает ключ; PATCH с полем secret-key подменяет ключ
# плейнтекстом из браузера. SOLUTION: чистый guard в mop/server/web.py --
# решение по (метод, путь, тело): пропустить / запретить / инжектировать
# management-блок из серверного источника. Парсинг -- только server-side,
# сырой ключ в телах и ответах не появляется.
# STATUS: FIXED — see #417
import json as _json417


def check_guard_417(c):
    fn = getattr(web, "guard_management_write", None)
    if not c.check("#417 web.guard_management_write exists", fn is not None):
        return
    # Источник ключа -- перечитываемый закрытый файл (#418 положит его в
    # каталог MOP_USER); callable, как файл: сбой чтения -- None.
    KEY = "k" * 32
    reads = []

    def source():
        reads.append(1)
        return KEY

    # GET и прочие безопасные методы -- прозрачны.
    for method, path in (("GET", "/v8/management/config"),
                         ("GET", "/v8/management/observability/logs"),
                         ("POST", "/v8/management/credentials/refresh")):
        got = fn(method, path, b"", source)
        c.expect(f"#417 {method} passes through", got["action"], "pass")

    # PATCH без secret-key -- пропуск как есть.
    method, path = "PATCH", "/v8/management/config"
    body = _json417.dumps({"management": {"disable-auto-update-panel": False},
                           "server": {"port": 18317}}).encode()
    got = fn(method, path, body, source)
    c.expect("#417 a merge PATCH without the secret passes", got["action"], "pass")

    # PATCH с secret-key (плейнтекст ИЛИ хэш) -- 403, до upstream.
    for what, value in (("plaintext", "NEWKEY"), ("the hash", "$2b$10$abc"),
                        ("empty", "")):
        body = _json417.dumps({"management": {"secret-key": value}}).encode()
        got = fn(method, path, body, source)
        c.expect(f"#417 PATCH carrying secret-key ({what}) is refused",
                 (got["action"], got.get("status")), ("refuse", 403))

    # path-addressed PATCH к самому полю -- тоже 403.
    body = _json417.dumps({"secret-key": "x"}).encode()
    got = fn("PATCH", "/v8/management/config/management", body, source)
    c.expect("#417 a path-addressed write to the field is refused",
             (got["action"], got.get("status")), ("refuse", 403))
    got = fn("PATCH", "/v8/management/config/management/secret-key", b'"y"', source)
    c.expect("#417 the leaf path itself is refused", (got["action"], got.get("status")),
             ("refuse", 403))

    # PUT полного документа БЕЗ блока management -- инжект блока с ключом
    # (вариант A: ключ перечитывается из источника на каждый write).
    method, path = "PUT", "/v8/management/config"
    body = _json417.dumps({"server": {"port": 18317}, "config-version": 8}).encode()
    got = fn(method, path, body, source)
    c.check("#417 a full PUT without management injects key+defaults",
            got["action"] == "inject"
            and _json417.loads(got["body"])["management"]["secret-key"] == KEY
            and _json417.loads(got["body"])["management"]["allow-remote"] is False, got)

    # Ключ перечитывается на каждый write: ротация файлом #418 подхватывается.
    before = len(reads)
    fn(method, path, body, source)
    c.check("#417 the key source is re-read per write", len(reads) == before + 1, len(reads))

    # PUT/config.yaml (raw YAML) без блока -- инжект YAML-строкой; с полем -- 403.
    yaml_body = b"server:\n  port: 18317\n"
    got = fn("PUT", "/v8/management/config.yaml", yaml_body, source)
    c.check("#417 a YAML PUT without management injects key+defaults",
            got["action"] == "inject"
            and b"management:" in got["body"] and b"allow-remote: false" in got["body"]
            and KEY.encode() in got["body"], got)
    yaml_bad = b"server:\n  port: 18317\nmanagement:\n  secret-key: \"K\"\n"
    got = fn("PUT", "/v8/management/config.yaml", yaml_bad, source)
    c.expect("#417 a YAML PUT carrying the secret is refused",
             (got["action"], got.get("status")), ("refuse", 403))

    # Битые тела write -- fail closed, не пропускать и не угадывать.
    for method, path in (("PATCH", "/v8/management/config"),
                         ("PUT", "/v8/management/config"),
                         ("PUT", "/v8/management/config.yaml")):
        got = fn(method, path, b"{not json", source)
        c.expect(f"#417 a malformed {method} body fails closed",
                 (got["action"], got.get("status")), ("refuse", 400))

    # Файл ключа недоступен, а PUT удалил бы секрет -- fail closed 503.
    got = fn("PUT", "/v8/management/config", body, lambda: None)
    c.expect("#417 no key source and a deleting PUT: refuse",
             (got["action"], got.get("status")), ("refuse", 503))

    # PUT С блоком management без secret-key -- инжект ключа, прочие
    # management-настройки клиента сохраняются (полная замена не теряет их).
    body = _json417.dumps({"management": {"disable-auto-update-panel": False},
                           "server": {"port": 18317}}).encode()
    got = fn("PUT", "/v8/management/config", body, source)
    c.check("#417 a PUT keeping other management settings gets the key injected",
            got["action"] == "inject"
            and _json417.loads(got["body"])["management"]["disable-auto-update-panel"] is False
            and _json417.loads(got["body"])["management"]["secret-key"] == KEY, got)


# Ответная сторона (#417): upstream отдаёт bcrypt-хэш secret-key в
# /config и /config.yaml -- в браузер не идёт ни хэш, ни ключ. Плейсхолдер
# знаком guard'у: явная запись плейсхолдера == отсутствию поля.
PLACEHOLDER = "managed-by-mop"


def check_guard_placeholder_417(c):
    fn = web.guard_management_write
    KEY = "k" * 32

    # GET-ответы: JSON и YAML ветки.
    red = getattr(web, "redact_management_read", None)
    if not c.check("#417 web.redact_management_read exists", red is not None):
        return
    body = _json417.dumps({"server": {"port": 18317},
                           "management": {"secret-key": "$2b$10$HASH",
                                          "allow-remote": False}}).encode()
    got = red("/v8/management/config", body)
    doc = _json417.loads(got)
    c.expect("#417 the JSON read hides the hash behind the placeholder",
             doc["management"]["secret-key"], PLACEHOLDER)
    c.expect("#417 other management fields survive the read",
             doc["management"]["allow-remote"], False)
    yaml_body = b"server:\n  port: 18317\nmanagement:\n  secret-key: \"$2b$10$HASH\"\n"
    got = red("/v8/management/config.yaml", yaml_body)
    c.check("#417 the YAML read hides the hash",
            b"$2b$10$HASH" not in got and PLACEHOLDER.encode() in got, got)
    untouched = b'{"credentials": {"files": []}}'
    c.expect("#417 a non-config read passes byte-for-byte",
             red("/v8/management/credentials", untouched), untouched)

    # Guard: плейсхолдер в клиентском write == поле отсутствует -> инжект;
    # любой иной явный ключ -> 403.
    body = _json417.dumps({"management": {"secret-key": PLACEHOLDER,
                                          "disable-auto-update-panel": False}}).encode()
    got = fn("PUT", "/v8/management/config", body, lambda: KEY)
    c.check("#417 the placeholder in a PUT reads as absent: key injected",
            got["action"] == "inject"
            and _json417.loads(got["body"])["management"]["secret-key"] == KEY
            and _json417.loads(got["body"])["management"]["disable-auto-update-panel"] is False,
            got)
    body = _json417.dumps({"management": {"secret-key": "$2b$10$HASH"}}).encode()
    got = fn("PUT", "/v8/management/config", body, lambda: KEY)
    c.expect("#417 the hash echoed back is an explicit key: refuse",
             (got["action"], got.get("status")), ("refuse", 403))
    got = fn("PUT", "/v8/management/config.yaml",
             f"management:\n  secret-key: {PLACEHOLDER}\n".encode(), lambda: KEY)
    c.check("#417 the placeholder in a YAML PUT reads as absent",
            got["action"] == "inject"
            and KEY.encode() in got["body"], got)


# ── #417: circuit breaker ключа ─────────────────────────────────────────
# CPA банит 127.0.0.1 на 30 минут после 5 неудач: панель одним загрузом
# шлёт больше пяти management-запросов, потому «не ретраить в рамках
# запроса» не спасает. Гейтвей после upstream 401/403 гасит ВСЕ запросы
# с тем же ключом до смены содержимого защищённой копии.
# STATUS: FIXED — see #417
def check_breaker_417(c):
    cls = getattr(web, "KeyBreaker", None)
    if not c.check("#417 web.KeyBreaker exists", cls is not None):
        return

    ups = []      # походы к upstream
    refusals = [] # локальные отказы
    br = web.KeyBreaker(ask=lambda: refusals.append(1))

    class Up:
        def __init__(self, code):
            self.code = code
        def __init__(self, code, log=None):
            self.code, self.log = code, (log if log is not None else [])
        def __call__(self, method, path, body, key):
            self.log.append(key)
            return self.code, b"upstream said no"

    # 401: первая попытка уходит, вторая с тем же ключом -- 503 без похода
    up = Up(401, ups)
    got = br("GET", "/v8/management/config", b"", "K1", up)
    c.expect("#417 the first 401 reaches upstream", got, (401, b"upstream said no"))
    for i in range(6):
        got = br("GET", "/v8/management/config", b"", "K1", up)
        c.expect(f"#417 after a 401 the same key is refused locally ({i})",
                 got, (503, b"proxy management key rejected"))
    c.expect("#417 exactly one upstream call for a rejected key", len(ups), 1)
    c.check("#417 later refusals never reach upstream", len(refusals) == 6, len(refusals))

    # новый ключ (ротация копии) -- проба снова разрешена
    up2 = Up(200, ups)
    got = br("GET", "/v8/management/config", b"", "K2", up2)
    c.expect("#417 a rotated key gets a fresh attempt", got, (200, b"upstream said no"))

    # 200 не активирует ничего (проверяем ДО 403: его cooldown глобален --
    # петля забанена, и даже хороший ключ не должен долбить upstream)
    up4 = Up(200, ups)
    br("GET", "/v8/management/config", b"", "K4", up4)
    got = br("GET", "/v8/management/config", b"", "K4", up4)
    c.expect("#417 a good key keeps flowing", got, (200, b"upstream said no"))

    # 403-бан: тот же ключ долбить нельзя, и бан переживёт ротацию ключа
    up3 = Up(403, ups)
    br("GET", "/v8/management/config", b"", "K3", up3)
    got = br("GET", "/v8/management/config", b"", "K3", up3)
    c.expect("#417 a 403 ban also trips the breaker", got, (503, b"proxy management key rejected"))
    got = br("GET", "/v8/management/config", b"", "K3-rotated", up3)
    c.expect("#417 the loopback ban survives a key rotation (cooldown)",
             got, (503, b"proxy management key rejected"))


# ── #417: HTTP-гейтвей управления прокси ────────────────────────────────
# HYPOTHESIS: браузеру нужен путь к панели CPA без ключа в браузере.
# SOLUTION: mop-web маршруты /llm/management.html (шим с sentinel) и
# /v8/management/* (проксирование с серверным ключом), /llm/raw/* (сток
# панель), все под admin-cookie; Bearer клиента снимается, ответы
# редачатся, write'ы идут через guard, ключ из файла #418.
# STATUS: FIXED — see #417
class _Upstream417:
    """Фикстура upstream: считает запросы, помнит последний Bearer/тело."""

    def __init__(self, code=200, body=b'{"ok": true}', redirect=None):
        self.code, self.body, self.redirect = code, body, redirect
        self.calls = []

    def __call__(self, method, path, body, key):
        self.calls.append({"method": method, "path": path, "body": body,
                           "key": key})
        if self.redirect:
            return 302, self.redirect
        return self.code, self.body


def _fetch_panel_417(upstream, key):
    """Станд-панель c одним относительным редиректом (как в контракте)."""
    code, got = upstream("GET", "/management.html", b"", key)
    if code in (301, 302, 303, 307, 308):
        target = got.decode("utf-8", "replace")
        if "://" in target or target.startswith("//"):
            return 502, b"proxy management redirect refused"
        if target.count("/") > 4 or len(target) > 256:
            return 502, b"proxy management redirect refused"
        code, got = upstream("GET", target, b"", key)
        return code, got
    return code, got


def _gateway417(webcli, store, provider, upstream, key="K" * 32,
                path="/v8/management/config", method="GET", body=b"",
                headers=None, breaker=None):
    from mop.server import websession as _ws
    """Прогнать запрос через тестовый Handler с подменённым upstream."""
    import types
    def request_upstream(method, p, b, h, key=None):
        upstream.forwarded_headers = h
        return (*upstream(method, p, b, key), "application/json")
    gw = types.SimpleNamespace(
        key=(lambda: key) if key is not None else (lambda: None),
        breaker=breaker or web.KeyBreaker(),
        call=request_upstream,
        fetch_panel=lambda: _fetch_panel_417(upstream, key))
    with patched(webcli.config, get=lambda name, default=None: {"MOP_SERVER_LAN": "10.0.0.1",
                                                                "MOP_HTTPS_PORT": "443"}.get(name, default)), \
            patched(webcli, SESSIONS=store), patched(_ws, provider=lambda: provider), \
            patched(webcli, GATEWAY=gw):
        return _fake_handler(webcli, method, path, body, headers or {})


def check_gateway_417(c):
    from mop.cli.server import web as webcli
    from mop.server import websession
    store = websession.SessionStore(now=lambda: 1000.0)
    admin = store.open(identity.Identity("anton", "admin", ()))
    user = store.open(identity.Identity("olga", "user", ()))
    provider = FakeProvider()

    # Гость и user не допускаются ни к API, ни к шиму: sentinel не пишется.
    for path in ("/v8/management/config", "/llm/management.html",
                 "/llm/raw/management.html"):
        sent, out = _gateway417(webcli, store, provider, _Upstream417(),
                                path=path, method="GET")
        c.check(f"#417 {path}: a guest gets 401, not the panel",
                sent[0][1] == 401 and web.SENTINEL.encode() not in out,
                (sent[0][1], out[:60]))
    sent, out = _gateway417(webcli, store, provider, _Upstream417(),
                            path="/v8/management/config", method="GET",
                            headers={"Cookie": f"mop_session={user}"})
    c.expect("#417 a logged-in user gets 403", sent[0][1], 403)

    # Admin: проксирование подменяет клиентский Bearer серверным ключом.
    up = _Upstream417()
    sent, out = _gateway417(webcli, store, provider, up,
                            headers={"Cookie": f"mop_session={admin}",
                                     "Authorization": "Bearer EVIL"})
    c.expect("#417 admin reaches the API through the gateway", sent[0][1], 200)
    c.expect("#417 the client Bearer is replaced by the server key",
             up.calls[0]["key"], "K" * 32)
    c.check("#417 no-store on the proxied answer",
            any(k == "Cache-Control" and v == "no-store" for k, v in sent))

    # GET /config: хэш закрыт плейсхолдером.
    up = _Upstream417(body=_json417.dumps(
        {"management": {"secret-key": "$2b$10$HASH"}}).encode())
    sent, out = _gateway417(webcli, store, provider, up,
                            path="/v8/management/config",
                            headers={"Cookie": f"mop_session={admin}"})
    c.check("#417 the hash never reaches the browser",
            b"$2b$10$HASH" not in out
            and web.PLACEHOLDER.encode() in out, out[:80])

    # Write: guard решает до upstream; unsafe методы требуют Origin.
    up = _Upstream417()
    sent, out = _gateway417(webcli, store, provider, up,
                            path="/v8/management/config", method="PUT",
                            body=_json417.dumps({"server": {"port": 1}}).encode(),
                            headers={"Cookie": f"mop_session={admin}",
                                     "Content-Type": "application/json"})
    c.expect("#417 an unsafe write without Origin is refused",
             sent[0][1], 403)
    c.expect("#417 nothing reached upstream", len(up.calls), 0)
    sent, out = _gateway417(webcli, store, provider, up,
                            path="/v8/management/config", method="PUT",
                            body=_json417.dumps({"server": {"port": 1}}).encode(),
                            headers={"Cookie": f"mop_session={admin}",
                                     "Origin": "https://10.0.0.1",
                                     "Content-Type": "application/json"})
    c.check("#417 a full PUT goes through the guard: key injected upstream",
            up.calls and up.calls[0]["body"]
            and _json417.loads(up.calls[0]["body"])["management"]["secret-key"] == "K" * 32,
            up.calls[:1])

    # Роль отозвана -- та же cookie уже не админ.
    class _NowUser:
        def lookup(self, login):
            return identity.Identity(login, "user", ()) if login == "anton" else None
    sent, out = _gateway417(webcli, store, _NowUser(), _Upstream417(),
                            headers={"Cookie": f"mop_session={admin}"})
    c.expect("#417 a demoted admin is refused on the same cookie",
             sent[0][1], 403)

    # Breaker: 401 от upstream -- второй запрос 503, upstream не зовётся.
    up = _Upstream417(code=401)
    br = web.KeyBreaker()
    _gateway417(webcli, store, provider, up, breaker=br,
                headers={"Cookie": f"mop_session={admin}"})
    calls_before = len(up.calls)
    sent, out = _gateway417(webcli, store, provider, up, breaker=br,
                            headers={"Cookie": f"mop_session={admin}"})
    c.expect("#417 the breaker answers 503 locally", sent[0][1], 503)
    c.expect("#417 no extra upstream call after the breaker", len(up.calls),
             calls_before)
    c.check("#417 the refusal body carries no key",
            b"K" * 32 not in out, out[:60])

    # Нет ключа (#418 ещё не положила копию) -- чтение тоже 503.
    gw_upstream = _Upstream417()
    sent, out = _gateway417(webcli, store, provider, gw_upstream, key=None,
                            headers={"Cookie": f"mop_session={admin}"})
    c.expect("#417 without the key file even reads fail closed", sent[0][1], 503)
    c.expect("#417 upstream untouched without the key", len(gw_upstream.calls), 0)

    # Шим: admin получает страницу с sentinel, raw -- сток-панель без sentinel.
    up = _Upstream417(body=b"<!doctype html>stock panel")
    sent, out = _gateway417(webcli, store, provider, up,
                            path="/llm/management.html",
                            headers={"Cookie": f"mop_session={admin}"})
    c.check("#417 stock 8.0.8 migrates the public sentinel from managementKey",
            b"localStorage.setItem('managementKey'" in out, out[:350])
    c.check("#417 the shim bootstraps the sentinel for the admin",
            web.SENTINEL.encode() in out
            and b"/llm/raw/management.html" in out, out[:100])
    sent, out = _gateway417(webcli, store, provider, up,
                            path="/llm/raw/management.html",
                            headers={"Cookie": f"mop_session={admin}"})
    c.check("#417 the raw panel is served without the sentinel",
            out.startswith(b"<!doctype html>stock panel")
            and web.SENTINEL.encode() not in out, out[:80])

    # Редирект: относительный на ту же панель -- следуем; абсолютный -- нет.
    up = _Upstream417(redirect=b"/management.html")
    sent, out = _gateway417(webcli, store, provider, up,
                            path="/llm/raw/management.html",
                            headers={"Cookie": f"mop_session={admin}"})
    c.check("#417 a relative redirect is followed in-origin",
            up.calls and any(call["path"] == "/management.html" for call in up.calls),
            up.calls)
    up = _Upstream417(redirect=b"https://evil.example/panel")
    sent, out = _gateway417(webcli, store, provider, up,
                            path="/llm/raw/management.html",
                            headers={"Cookie": f"mop_session={admin}"})
    c.expect("#417 an absolute redirect fails closed", sent[0][1], 502)


# Параллельный край breaker (#417): до первого ответа панель уже открыла
# несколько management-запросов; если гасить ключ только после ответа,
# петля успевает попасть под бан CPA. Пробы ключа сериализуются, пока
# ключ не подтверждён.
def check_breaker_singleflight_417(c):
    import threading
    cls = web.KeyBreaker
    br = cls()
    ups = []

    class Slow401:
        def __call__(self, method, path, body, key):
            ups.append(key)
            import time as _t
            _t.sleep(0.05)
            return 401, b"no"

    results = []
    def worker():
        results.append(br("GET", "/v8/management/config", b"", "K1", Slow401()))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    c.check("#417 concurrent first attempts: at most one reaches upstream",
            len(ups) <= 1, ups)
    c.check("#417 all callers get the refusal",
            all(r == (503, cls.REFUSED_BODY) for r in results)
            or all(r[0] in (401, 503) for r in results), results[:3])

    # после подтверждённого успеха параллельность возвращается
    ups.clear()
    class Ok:
        def __call__(self, method, path, body, key):
            ups.append(key)
            return 200, b"yes"
    br2 = cls()
    results2 = []
    def worker2():
        results2.append(br2("GET", "/v8/management/config", b"", "K9", Ok()))
    threads = [threading.Thread(target=worker2) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    c.check("#417 a proven-good key flows in parallel",
            len(ups) == 4 and all(r[0] == 200 for r in results2), (len(ups), results2[:2]))


# Заголовки ответа upstream (#417): наружу идёт только whitelist --
# Set-Cookie/WWW-Authenticate/Access-Control-* не проходят.
def check_header_whitelist_417(c):
    fn = getattr(web, "response_headers", None)
    if not c.check("#417 web.response_headers exists", fn is not None):
        return
    given = {"Content-Type": "application/json",
             "Set-Cookie": "session=evil",
             "WWW-Authenticate": 'Basic realm="cpa"',
             "Access-Control-Allow-Origin": "*",
             "X-Cpa-Version": "8.0.8"}
    got = fn(given)
    c.expect("#417 the whitelist keeps content-type and version",
             (got.get("Content-Type"), got.get("X-Cpa-Version")),
             ("application/json", "8.0.8"))
    for bad in ("Set-Cookie", "WWW-Authenticate", "Access-Control-Allow-Origin"):
        c.check(f"#417 {bad} never passes to the browser", bad not in got, got)



# ── #417: сетевой контракт шлюза при ошибках и перенаправлении ──────────
# HYPOTHESIS: urllib автоматически переходит по 302 с Authorization на
# другой origin; обрыв первого upstream до ответа оставляет single-flight
# в исключении, а битый конфиг с хэшем уходит браузеру как есть.
# SOLUTION: открывать upstream без автоперехода; переход обрабатывать только
# по Location на разрешённый путь, секретные ответы fail closed; breaker
# завершает пробу и выпускает ожидающих даже при исключении.
# STATUS: FIXED — see #417

def check_gateway_fail_closed_417(c):
    import threading as _threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from unittest import mock
    from mop.cli.server import web as webcli

    leaks = []
    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            leaks.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"remote")
        def log_message(self, *args):
            pass
    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/remote")
            self.end_headers()
        def log_message(self, *args):
            pass
    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    workers = [_threading.Thread(target=s.serve_forever, daemon=True)
               for s in (target, redirect)]
    for worker in workers:
        worker.start()
    try:
        gw = webcli.Gateway(key="only-on-server")
        gw.port = lambda: redirect.server_port
        with mock.patch.dict(os.environ, {"HTTP_PROXY": f"http://127.0.0.1:{target.server_port}",
                                              "http_proxy": f"http://127.0.0.1:{target.server_port}",
                                              "NO_PROXY": "", "no_proxy": ""}):
            code, body, _ = gw.call("GET", "/management.html", b"")
        c.check("#417 HTTP_PROXY never sees the server key or the request",
                not leaks, leaks)
        c.expect("#417 redirect remains an explicit response to the caller", code, 302)
    finally:
        for server in (redirect, target):
            server.shutdown()
            server.server_close()

    probes = []
    class WrongKey(BaseHTTPRequestHandler):
        def do_GET(self):
            probes.append(self.path)
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"wrong key")
        def log_message(self, *args):
            pass
    wrong = ThreadingHTTPServer(("127.0.0.1", 0), WrongKey)
    worker = _threading.Thread(target=wrong.serve_forever, daemon=True)
    worker.start()
    try:
        gw = webcli.Gateway(key="rejected-key")
        gw.port = lambda: wrong.server_port
        first = gw.fetch_panel()
        second = gw.fetch_panel()
        c.expect("#417 raw panel key mismatch triggers one probe, not an IP ban",
                 len(probes), 1)
        c.expect("#417 the second raw panel request fails closed locally",
                 second[0], 503)
    finally:
        wrong.shutdown()
        wrong.server_close()

    br = web.KeyBreaker()
    def fails(*args):
        raise RuntimeError("upstream died")
    try:
        br("GET", "/v8/management/config", b"", "K", fails)
    except RuntimeError:
        pass
    except Exception as exc:
        c.fail(f"#417 breaker must preserve upstream failure, got {type(exc).__name__}")
    else:
        c.fail("#417 upstream failure should propagate")
    c.check("#417 failed probe does not strand an inflight event",
            not br._inflight, br._inflight)
    c.check("#417 malformed config does not go to the browser",
            web.redact_management_read("/v8/management/config",
               b'{"management":{"secret-key":"$2b$HASH"}broken') is None)


def check_gateway_body_bounds_417(c):
    from mop.cli.server import web as webcli
    from mop.server import websession
    store = websession.SessionStore(now=lambda: 1000.0)
    token = store.open(identity.Identity("anton", "admin", ()))
    up = _Upstream417()
    good = {"Cookie": f"mop_session={token}",
            "Origin": "https://10.0.0.1", "Content-Type": "application/json"}
    sent, out = _gateway417(webcli, store, FakeProvider(), up,
                             method="POST", path="/v8/management/config",
                             body=b"{}", headers={**good, "Content-Length": "not-a-number"})
    c.expect("#417 malformed Content-Length gets a bounded rejection", sent[0][1], 400)
    c.expect("#417 malformed body length never reaches upstream", len(up.calls), 0)
    sent, out = _gateway417(webcli, store, FakeProvider(), up,
                             method="POST", path="/v8/management/config",
                             body=b"{}", headers={**good, "Content-Length": str(web.PANEL_MAX+1)})
    c.expect("#417 oversize Content-Length is rejected without truncation", sent[0][1], 413)
    c.expect("#417 oversized body never reaches upstream", len(up.calls), 0)


# ── #417: полный URL запроса и удаление management-конфига ──────────────
# HYPOTHESIS: query теряется при разборе маршрута Handler, а DELETE либо
# POST /config проходят мимо write-guard и способны удалить ключ.
# SOLUTION: маршрутизировать по pathname, к loopback передавать полный
# request-target и отвергать неразрешённые замены конфига до upstream.
# STATUS: FIXED — see #417

def check_gateway_route_and_delete_417(c):
    from mop.cli.server import web as webcli
    from mop.server import websession
    source = lambda: "K" * 32
    for method in ("DELETE", "POST"):
        got = web.guard_management_write(method, "/v8/management/config", b"{}", source)
        c.expect(f"#417 {method} cannot replace or erase the management key",
                 (got["action"], got.get("status")), ("refuse", 403))
    store = websession.SessionStore(now=lambda: 1000.0)
    token = store.open(identity.Identity("anton", "admin", ()))
    up = _Upstream417()
    headers = {"Cookie": f"mop_session={token}"}
    sent, out = _gateway417(webcli, store, FakeProvider(), up,
                             path="/v8/management/logs?limit=13%26page=2", headers=headers)
    c.expect("#417 management API forwards the exact query to loopback",
             up.calls[0]["path"] if up.calls else None,
             "/v8/management/logs?limit=13%26page=2")
    sent, out = _gateway417(webcli, store, FakeProvider(), up,
                             method="PUT", path="/v8/management/config.yaml",
                             body=b"server: {port: 1}\n",
                             headers={**headers, "Origin": "https://10.0.0.1",
                                      "Content-Type": "application/yaml"})
    c.expect("#417 Source Save keeps YAML media type to loopback",
             up.forwarded_headers.get("Content-Type"), "application/yaml")
    sent, out = _gateway417(webcli, store, FakeProvider(), up,
                             method="DELETE", path="/v8/management/config",
                             headers={**headers, "Origin": "https://10.0.0.1"})
    c.expect("#417 blocked DELETE does not reach upstream", len(up.calls), 2)

def main():
    c = Checks()
    for fn in (check_classify, check_projects, check_sizes, check_journal, check_usage,
               check_snapshot, check_sick_in_project_210, check_by_user_245,
               check_masters_305, check_dist_297,
               check_rounds_319, check_replies_319, check_who_319, check_shape_319,
               check_load_378, check_master_sessions_421, check_who_answer_421,
               check_session_store_415, check_login_api_415,
               check_address_source_415, check_deleted_user_415,
               check_journal_api_424, check_journal_entry_424,
               check_login_bounds_415, check_guard_417,
               check_guard_placeholder_417, check_breaker_417,
               check_gateway_417, check_breaker_singleflight_417,
               check_header_whitelist_417, check_gateway_fail_closed_417,
               check_gateway_body_bounds_417, check_gateway_route_and_delete_417):
        fn(c)
    return c.report("web")


if __name__ == "__main__":
    sys.exit(main())
