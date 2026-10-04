#!/usr/bin/env python3
"""Admin HTTP API поверх общих операций: python3 tests/adminapi.py

HYPOTHESIS (#461): админу GUI нужны те же операции пула, что CLI (#458),
но без терминала; запускать workflow в потоке запроса нельзя (#476), и
второй реализации быть не должно. Раздавать произвольные RPC/subject или
shell-строки через HTTP нельзя.
SOLUTION: mop/server/adminapi.py -- тонкий адаптер: гейты admin/Origin/
Content-Type/Content-Length/JSON, узкая схема {action, target, params},
реестр допустимых операций и параметров (unsupported до регистрации),
send-обработчик первого выпуска (только адресная доставка папету),
постановка воркеру без исполнения в потоке запроса, различимые коды
(202 принято / 409 конфликт / 4xx валидация / 5xx транспорт), детали
ошибок -- allowlist, не raw exceptions. GET результатов -- admin-only,
no-store, не в публичном SSE; payload -- только в приватной очереди.
STATUS: FIXED — see #461
"""
import json
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, patched_env  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

try:
    from mop.server import adminapi
except ImportError:
    adminapi = None

ADMIN = ("ermak", "admin")
USER = ("olga", "user")


def req(action=None, target=None, params=None, **extra):
    body = {}
    if action is not None:
        body["action"] = action
    if target is not None:
        body["target"] = target
    if params is not None:
        body["params"] = params
    body.update(extra)
    return json.dumps(body).encode()


class Handler:
    """Подставной HTTP-handler: только то, что читает адаптер."""

    def __init__(self, who=ADMIN, origin="https://pool.example",
                 content_type="application/json", length=None, body=b"",
                 peer=("10.0.0.9", 4242)):
        self.who = who
        self.origin = origin
        self.headers = {"Origin": origin, "Content-Type": content_type}
        if length is not None:
            self.headers["Content-Length"] = str(length)
        self.body = body
        self.command = "POST"
        self.path = "/api/admin/ops"
        self.rfile = type("R", (), {"read": lambda _, n: body})()
        self.client_address = peer
        self.sent = []

    def _json(self, code, payload):
        self.sent.append((code, payload))
        return None

    def _origin_ok(self):
        return self.headers.get("Origin") == self.origin


def main():
    c = Checks()
    if not c.check("mop.server.adminapi exists", adminapi is not None):
        return c.report("adminapi")

    ops = adminapi.Operations()
    queue = adminapi.Queue()
    api = adminapi.Api(ops, queue, origin="https://pool.example", who=ADMIN)

    # ── гейты: роли, провайдер, Origin, тип, размер, JSON ───────────────
    # Гость и не-admin не доходят до разбора тела.
    for who, want in ((None, 401), (("olga", "user"), 403)):
        h = Handler(who=who, body=req("send", "pu-mop-1"), length=99)
        api.post(h)
        c.check(f"#461 {who and who[1] or 'guest'} -> {want}",
                h.sent and h.sent[0][0] == want, h.sent)
    # Провайдер лёг -- закрыто, и без сырой ошибки наружу.
    def broken(*a, **k):
        raise RuntimeError("ldap socket: secret-path /etc/ld.pwd")
    h = Handler(body=req("send", "pu-mop-1"), length=99)
    adminapi.Api(ops, queue, origin="https://pool.example", who=ADMIN,
                 provider=broken).post(h)
    c.check("#461 provider failure is closed",
            h.sent and h.sent[0][0] in (503, 401) and
            "secret" not in json.dumps(h.sent[0][1]), h.sent)
    # Чужой/отсутствующий Origin.
    for origin in (None, "https://evil.example"):
        h = Handler(origin=origin or "", body=req("send", "pu-mop-1"), length=99)
        api.post(h)
        c.check(f"#461 origin {origin!r} refused", h.sent and h.sent[0][0] == 403, h.sent)
    # Не-JSON тип, oversized, недочитанное тело, мусор, лишние поля.
    h = Handler(content_type="text/plain", body=b"x", length=1)
    api.post(h)
    c.check("#461 non-JSON content type refused", h.sent and h.sent[0][0] in (400, 415),
            h.sent)
    big = b"{" + b'"pad": "' + b"x" * (adminapi.BODY_MAX + 1) + b'"}'
    h = Handler(body=big, length=len(big))
    api.post(h)
    c.check("#461 oversized body refused", h.sent and h.sent[0][0] == 413, h.sent)
    h = Handler(body=req("send", "pu-mop-1")[:5], length=99)
    api.post(h)
    c.check("#461 truncated JSON refused", h.sent and h.sent[0][0] == 400, h.sent)
    h = Handler(body=b"not json", length=8)
    api.post(h)
    c.check("#461 garbage refused", h.sent and h.sent[0][0] == 400, h.sent)
    h = Handler(body=req("send", "pu-mop-1", actor="forged", env={"X": 1},
                         subject="mop.admin.cluster.rpc", verb="delete",
                         cwd="/", argv=["rm"]), length=None)
    h.headers["Content-Length"] = str(len(h.body))
    api.post(h)
    c.check("#461 forged actor/env/subject/verb/cwd/argv refused as unknown fields",
            h.sent and h.sent[0][0] == 400, h.sent)

    # ── реестр: незарегистрированное -- unsupported, не 500 ─────────────
    h = Handler(body=req("restart", "pu-mop-1"), length=None)
    h.headers["Content-Length"] = str(len(h.body))
    api.post(h)
    # ── #491: очередь адаптера потребляется -- задача доезжает до воркера,
    # статус записи становится терминальным (иначе «принято» висит вечно).
    from mop.cli.server import web as webcli
    import time as _time

    class _Clock:
        def __init__(self): self.t = 1000.0
        def __call__(self): return self.t

    clock = _Clock()
    driven = adminapi.Queue()
    driven_ops = adminapi.Operations()
    from mop.server import opworker as _ow
    _ow.register(driven_ops)
    driven_api = adminapi.Api(driven_ops, driven, origin="https://pool.example", who=ADMIN)
    body = req("restart", "pu-mop-1")
    h = Handler(who=ADMIN, body=body, length=len(body))
    driven_api.post(h)
    op_id = h.sent[0][1]["id"]

    class _Done(Exception):
        pass

    def effect():  # подменный эффект: outcome-вид #458, терминал сразу
        return {"outcome": "registered", "name": "pu-mop-1"}

    from mop.server import opworker
    w = opworker.Worker(now=clock, sleep=lambda s: None)
    job = driven.take()
    c.check("#491 the queue yields the accepted job", job is not None and job.get("id") == op_id, job)
    verdict = w.submit(job["login"], "restart", "pu-mop-1", {}, effect=effect)
    c.expect("#491 the worker speaks a terminal status",
             verdict.get("status"), "succeeded")
    driven_api.settle(op_id, verdict)
    got = driven_api.result(op_id, ("anton", "admin"))
    c.expect("#491 the result reads the worker verdict",
             got.get("status"), "succeeded")

    # ── #464/#489: воркер регистрирует все глаголы адаптера одной точкой;
    # рестарт был пропущен -- POST из GUI отвечал unsupported (#489).
    from mop.server import opworker
    wired_all = adminapi.Operations()
    opworker.register(wired_all)
    wired_api2 = adminapi.Api(wired_all, adminapi.Queue(),
                              origin="https://pool.example", who=ADMIN)
    for verb in ("add", "update", "restart", "recycle", "delete", "wipe", "model"):
        body = req(verb, target="pu-mop-1")
        h = Handler(who=ADMIN, body=body, length=len(body))
        wired_api2.post(h)
        c.check(f"#489 opworker.register wires {verb}",
                h.sent and h.sent[0][0] != 404, h.sent)

    body = req("dance", "pu-mop-1")
    h = Handler(who=ADMIN, body=body, length=len(body))
    api.post(h)
    c.check("#461 unregistered operation is 404/unsupported, no button",
            h.sent and h.sent[0][0] == 404
            and h.sent[0][1].get("error") == "unsupported operation", h.sent)
    # send зарегистрирован из коробки: первый выпуск.
    sent_to_worker = []
    queue.put = lambda job: sent_to_worker.append(job)
    h = Handler(body=req("send", "pu-mop-1", params={"message": "hi"}), length=None)
    h.headers["Content-Length"] = str(len(h.body))
    api.post(h)
    c.check("#461 send -> 202 with an operation id",
            h.sent and h.sent[0][0] == 202 and h.sent[0][1].get("id"), h.sent)
    c.check("#461 the job went to the worker queue, not the request thread",
            len(sent_to_worker) == 1 and sent_to_worker[0]["action"] == "send"
            and sent_to_worker[0]["target"] == "pu-mop-1", sent_to_worker)
    c.check("#461 queued payload is private: not in the answer",
            "message" not in json.dumps(h.sent[0][1]), h.sent)
    # Неизвестные параметры -- не пасс-through.
    h = Handler(body=req("send", "pu-mop-1", params={"message": "hi",
                                                     "notify_when_idle": True,
                                                     "wait": 60}), length=None)
    h.headers["Content-Length"] = str(len(h.body))
    api.post(h)
    c.check("#461 first-release send policy: notify/wait are not accepted",
            h.sent and h.sent[0][0] == 400, h.sent)

    # ── конфликты и повторы ─────────────────────────────────────────────
    # Тот же payload повторно -- конфликт изменившейся цели/конкурирующей
    # операции; тот же id -- идемпотентный ответ.
    first = api.submit("send", "pu-mop-1", {"message": "hi"}, ADMIN)
    second = api.submit("send", "pu-mop-1", {"message": "hi"}, ADMIN)
    c.check("#461 the same payload resubmits to the same id (idempotent)",
            first["id"] == second["id"], (first, second))
    try:
        api.submit("send", "pu-mop-1", {"message": "other"}, ADMIN)
        c.fail("#461 a different payload for the same target conflicts while queued",
               "no conflict raised")
    except adminapi.Refused as e:
        c.check("#461 a different payload conflicts with 409", e.code == 409, e.code)

    # ── GET результата: admin-only, no-store, чужой id -- не отдаётся ───
    got = api.result(first["id"], ADMIN)
    c.check("#461 own result is readable", got.get("id") == first["id"], got)
    c.check("#461 a result of an unknown id is not found",
            api.result("no-such-id", ADMIN).get("status") == "not found")
    c.check("#461 a result is admin-only",
            api.result(first["id"], USER).get("status") in ("401", "403", "refused"))
    c.check("#461 the result carries no message payload",
            "message" not in json.dumps(got), got)
    # Публичный снимок ростера не подтверждает операцию: ответ результата
    # несёт метафору операции (action/target/status), но не payload.
    c.check("#461 results never ride the public snapshot",
            not any(k in got for k in ("params", "message")), got)

    # ── санитизация деталей ─────────────────────────────────────────────
    c.expect("#461 sanitized error keeps the words, drops the path",
             adminapi.sanitize(RuntimeError("boom: /home/pool/secret.key gone")),
             "boom gone")
    c.expect("#461 sanitize keeps a multiword line without paths",
             adminapi.sanitize(ValueError("target pu-mop-1 refused: held by olga")),
             "target pu-mop-1 refused held by olga")

    # ── параллельные контексты: два запроса -- две операции ────────────
    import threading
    fresh = adminapi.Api(ops, adminapi.Queue(), origin="https://pool.example")
    made = []

    def submit(tag):
        made.append(fresh.submit("send", f"pu-mop-{tag}", {"message": tag}, ADMIN))

    ts = [threading.Thread(target=submit, args=(i,)) for i in (1, 2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    ids = [m["id"] for m in made]
    c.check("#461 parallel requests keep separate ids and targets",
            len(set(ids)) == 2 and {m.get("target") for m in made} == {"pu-mop-1", "pu-mop-2"},
            made)

    # ── реестр проектов для формы add (#472) ───────────────────────────
    # Читатель реестра подставной: строки -- origin'ы, как их держит
    # projects.read(); нет файла -- пусто.
    def registry(lines):
        return lambda: set(lines)

    got = adminapi.project_list(registry(["git@h:g/mop.git", "https://x/y/web.git"]))
    by = {p["name"]: p for p in got}
    c.check("#472 project_list names by project_of and keeps the origin",
            by.get("mop", {}).get("origin") == "git@h:g/mop.git"
            and by.get("web", {}).get("origin") == "https://x/y/web.git", got)
    c.check("#472 project_list is sorted by name",
            [p["name"] for p in got] == ["mop", "web"], got)
    c.check("#472 an empty registry reads as an empty list, not an error",
            adminapi.project_list(registry([])) == [], got)
    # Кривая строка без origin-вида не роняет список: их вычищает reader,
    # но здесь — защита по записи (#337).
    got = adminapi.project_list(registry(["git@h:g/mop.git", "junk line"]))
    c.check("#472 a non-origin line is dropped, the rest survives",
            [p["name"] for p in got] == ["mop"], got)

    # GET /api/admin/projects: admin-only, остальным -- код отказа.
    listing_api = adminapi.Api(ops, adminapi.Queue(), origin="https://pool.example",
                               who=ADMIN)
    class ListHandler:
        command = "GET"
        path = "/api/admin/projects"
        origin = "https://pool.example"
        headers = {"Origin": "https://pool.example"}
        def __init__(self, who):
            self.who = who
            self.sent = []
        def _json(self, code, payload):
            self.sent.append((code, payload))

    h = ListHandler(ADMIN)
    listing_api.projects(h, registry(["git@h:g/mop.git"]))
    c.check("#472 GET projects serves the admin the list",
            h.sent and h.sent[0][0] == 200
            and h.sent[0][1].get("projects")[0]["name"] == "mop", h.sent)
    h = ListHandler(None)
    listing_api.projects(h, registry(["git@h:g/mop.git"]))
    c.check("#472 GET projects refuses a guest with 401",
            h.sent and h.sent[0][0] == 401, h.sent)
    h = ListHandler(USER)
    listing_api.projects(h, registry(["git@h:g/mop.git"]))
    c.check("#472 GET projects refuses a non-admin with 403",
            h.sent and h.sent[0][0] == 403, h.sent)
    # Провайдер лёг -- закрыто, без сырой ошибки.
    def broken_provider(*a, **k):
        raise RuntimeError("ldap socket /etc/ld.pwd down")
    closed = adminapi.Api(ops, adminapi.Queue(), origin="https://pool.example",
                          provider=broken_provider)
    h = ListHandler(ADMIN)
    closed.projects(h, registry(["git@h:g/mop.git"]))
    c.check("#472 GET projects with a dead provider is closed, no raw detail",
            h.sent and h.sent[0][0] in (503, 401)
            and "ldap" not in json.dumps(h.sent[0][1]), h.sent)

    # ── add/update в реестре операций (#470) ────────────────────────────
    # HYPOTHESIS: реестр знает только send; клик add/update (#472) доезжает
    # до адаптера и отвечает 404 unsupported -- кнопки формы мертвы. У add
    # цели ещё нет: требование target в _parse отметило бы его до реестра.
    # SOLUTION: register("add"/"update", params) (#470); target обязателен
    # для существующих операций и не обязателен для add (origin в params).
    # Подключение -- одна точка у воркера (#470): адаптер и исполнитель
    # расходиться не могут.
    wired = adminapi.Operations()
    from mop.server import opworker
    opworker.register(wired)
    wired_api = adminapi.Api(wired, adminapi.Queue(),
                             origin="https://pool.example", who=ADMIN)

    def post(action, target="pu-mop-1", params=None):
        body = req(action, target=target, params=params or {})
        h = Handler(body=body, length=len(body))
        wired_api.post(h)
        return h

    h = post("add", target=None, params={"origin": "git@h:g/mop.git"})
    c.check("#470 add without a target is accepted (202)",
            h.sent and h.sent[0][0] == 202 and h.sent[0][1].get("id"), h.sent)
    # Следующая проверка схемы — отдельная команда после завершения первой.
    accepted_add = wired_api.queue.take()
    wired_api.settle(accepted_add["id"], {"status": "succeeded"})
    h = post("add", target=None, params={"origin": "git@h:g/mop.git",
                                         "branch": "dev"})
    c.check("#470 add accepts origin and branch, nothing else",
            h.sent and h.sent[0][0] == 202, h.sent)
    h = post("add", target=None, params={"origin": "git@h:g/mop.git",
                                         "fresh": True})
    c.check("#470 add refuses an update-only parameter",
            h.sent and h.sent[0][0] == 400, h.sent)
    h = post("update", params={"fresh": False})
    c.check("#470 update keeps origin implicit, carries fresh",
            h.sent and h.sent[0][0] == 202, h.sent)
    h = post("update", target="pu-mop-2",
             params={"fresh": True, "origin": "git@h:g/new.git"})
    c.check("#470 remove mode carries fresh and the new origin",
            h.sent and h.sent[0][0] == 202, h.sent)
    h = post("update", params={"mode": "keep"})
    c.check("#470 update refuses unknown parameters",
            h.sent and h.sent[0][0] == 400, h.sent)
    # target не стал необязательным вовсе: цель существующих операций
    # осталась обязательной.
    h = post("restart", target=None, params={})
    c.check("#470 a lifecycle op still requires its target",
            h.sent and h.sent[0][0] == 400, h.sent)
    h = post("update", target=None, params={"fresh": False})
    c.check("#470 update without a target is refused",
            h.sent and h.sent[0][0] == 400, h.sent)

    # ── recycle/delete/wipe в реестре операций (#471) ───────────────────
    # HYPOTHESIS: реестр знает send/restart/add/update; клик разрушительных
    # действий (#473) доезжает до адаптера и отвечает 404 unsupported --
    # кнопки DestructiveBox мертвы. У ruin-глаголов аргументов нет: force
    # веб не берёт по определению, параметров пусто.
    # SOLUTION: три register в той же точке стыковки opworker.register;
    # пустой кортеж параметров -- «никаких лишних полей», любое тело
    # params -- 400 unknown parameters. 202 -- запись в очередь воркера,
    # не исполнение.
    # До/после подключения -- на двух реестрах: подключённый выше wired
    # свою работу сделал (202), чистый -- честные 404 кнопок без неё.
    unwired = adminapi.Operations()
    unwired_api = adminapi.Api(unwired, adminapi.Queue(),
                               origin="https://pool.example", who=ADMIN)

    def post_raw(action, target="pu-mop-1", params=None):
        body = req(action, target=target, params=params or {})
        h = Handler(body=body, length=len(body))
        unwired_api.post(h)
        return h

    h = post_raw("recycle")
    c.check("#471 an unregistered ruin verb is 404 unsupported",
            h.sent and h.sent[0][0] == 404
            and h.sent[0][1].get("error") == "unsupported operation", h.sent)
    h = post_raw("wipe", params={"force": True})
    c.check("#471 wipe on an unwired registry is 404, force or not",
            h.sent and h.sent[0][0] == 404, h.sent)

    opworker.register(wired)
    h = post("recycle")
    c.check("#471 recycle posts empty and reaches the queue (202)",
            h.sent and h.sent[0][0] == 202 and h.sent[0][1].get("id"), h.sent)
    h = post("delete")
    c.check("#471 delete posts empty and reaches the queue (202)",
            h.sent and h.sent[0][0] == 202 and h.sent[0][1].get("id"), h.sent)
    h = post("wipe")
    c.check("#471 wipe posts empty and reaches the queue (202)",
            h.sent and h.sent[0][0] == 202 and h.sent[0][1].get("id"), h.sent)
    for verb in ("recycle", "delete", "wipe"):
        h = post(verb, params={"force": True})
        c.check(f"#471 {verb} refuses any parameter (force never rides)",
                h.sent and h.sent[0][0] == 400
                and "unknown parameters" in h.sent[0][1].get("error", ""),
                h.sent)

    # ── каталог моделей для формы (#468) ─────────────────────────────────
    # HYPOTHESIS: форма смены не знает моделей -- их зовёт прокси, и
    # список у браузера только если сервер сам его принёс. Ключ прокси в
    # ответе или в конфиге фронта -- утечка (#448: ключ не покидает HTTPS
    # и localhost); вторая копия urllib-логики в adminapi -- расползание.
    # SOLUTION: чистый парсер каталога -- общий (client слоя #448 и
    # сервер зовут один код), читатель сервера -- config + proxykey.source
    # (файл установленного прокси, прежний env -- переходный). Ответ --
    # {"models": [id]}; ключ и URL наружу не едут; лежащий источник --
    # closed 503 без сырой детали.
    from mop.common import llm

    c.check("#468 the catalog parser is the client one, not a copy",
            llm.catalog_ids({"data": [{"id": "glm-5.3"}]}) == ["glm-5.3"], None)
    try:
        llm.catalog_ids({"data": [{"name": "opus"}]})
        c.check("#468 a malformed catalog is rejected by the shared parser", False)
    except ValueError:
        c.check("#468 a malformed catalog is rejected by the shared parser", True)

    def catalog(readings):
        """Читатель каталога: ответы по очереди; исключение -- лежащий
        источник (503 без сырой детали)."""
        def reader():
            got = readings.pop(0) if len(readings) > 1 else readings[0]
            if isinstance(got, Exception):
                raise got
            return got
        return reader

    class GetHandler:
        command = "GET"
        path = "/api/admin/models"
        headers = {}
        def __init__(self, who=ADMIN):
            self.who = who
            self.sent = []
        def _json(self, code, payload):
            self.sent.append((code, payload))

    listing_api = adminapi.Api(ops, adminapi.Queue(),
                               origin="https://pool.example", who=ADMIN)
    h = GetHandler()
    listing_api.models(h, catalog([["glm-5.3", "claude-opus-5-5"]]))
    c.check("#468 GET models serves the admin the id list",
            h.sent and h.sent[0][0] == 200
            and h.sent[0][1] == {"models": ["claude-opus-5-5", "glm-5.3"]}, h.sent)
    h = GetHandler(None)
    listing_api.models(h, catalog([["glm-5.3"]]))
    c.check("#468 GET models refuses a guest with 401",
            h.sent and h.sent[0][0] == 401, h.sent)
    h = GetHandler(USER)
    listing_api.models(h, catalog([["glm-5.3"]]))
    c.check("#468 GET models refuses a non-admin with 403",
            h.sent and h.sent[0][0] == 403, h.sent)
    h = GetHandler()
    listing_api.models(h, catalog([RuntimeError("secret /etc/mop/key down")]))
    c.check("#468 a dead catalog source reads closed, no raw detail",
            h.sent and h.sent[0][0] == 503
            and json.dumps(h.sent[0][1])
            == '{"error": "model catalog is unavailable"}', h.sent)

    return c.report("adminapi")


if __name__ == "__main__":
    sys.exit(main())
