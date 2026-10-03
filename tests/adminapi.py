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
from _lib import Checks  # noqa: E402
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

    return c.report("adminapi")


if __name__ == "__main__":
    sys.exit(main())
