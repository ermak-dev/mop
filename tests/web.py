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
from _lib import Checks, patched  # noqa: E402
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
                # тот же мастер ответил дважды (две подписки) -- одна строка
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

def _fake_handler(webcli, method, path, body=b""):
    import io
    h = webcli.Handler.__new__(webcli.Handler)
    h.path, h.headers = path, {"Content-Length": str(len(body))}
    h.rfile, h.wfile, h.sent = io.BytesIO(body), io.BytesIO(), []
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
    rows = [PuppetRow("pu-mop-1", "n1", "running", "busy", "busy", "anton", "claude",
                      "git@h:g/mop.git")]
    c.expect("#319 page: a who answer as a masters row",
             web.master_rows({"mop": [answer, bare]}, rows),
             [{"project": "mop", "master": "-", "user": "-", "session": "-", "cwd": "-", "puppets": []},
              {"project": "mop", "master": "anton.mate-7", "user": "anton", "session": "s-1",
               "cwd": "/w", "puppets": ["pu-mop-1"]}])
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


def main():
    c = Checks()
    for fn in (check_classify, check_projects, check_sizes, check_journal, check_usage,
               check_snapshot, check_sick_in_project_210, check_by_user_245,
               check_masters_305, check_dist_297,
               check_rounds_319, check_replies_319, check_who_319, check_shape_319,
               check_load_378):
        fn(c)
    return c.report("web")


if __name__ == "__main__":
    sys.exit(main())
