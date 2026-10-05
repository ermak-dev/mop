#!/usr/bin/env python3
"""Тонкий исполнитель веб-операций: python3 tests/adminapi.py

HYPOTHESIS (#499): веб держал две копии «жизни операции» -- очередь задач,
_active-замок с 409 и _results с TTL (адаптер #461), журнал ops.jsonl,
payload-файлы, _recover и пер-целевую очередь (воркер #476). Общий workflow
#458 уже один, а копии оркестрации разъехались (#497: вечный accepted,
409 часами).
SOLUTION: исполнитель #499 в adminapi.Api -- приём в памяти (идемпотентность
тем же payload -> тот же id), поток на операцию через ops.* тем же
транспортом, что CLI, сериализация семафором на цель (ждёт, а не отказывает),
терминал -- SSE-кадром operation в существующий поток /events (#424),
рестарт службы -- честный unknown с ссылкой на журнал: правда об эффекте --
аудит пула (ops.record) и /api/journal. Семафор освобождается в finally:
таймаут зависшего эффекта #497 встанет сюда без переделки слоя.
"""
import json
import os
import sys
import threading
import time

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.server import adminapi, opworker  # noqa: E402

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


class Scripted:
    """Эффект-подмена: считанные вызовы, сценарии ответов.

    gate -- барьер медленного эффекта: отпускает его проверка; entered --
    факт захода в эффект (вторая операция обязана ждать семафор цели,
    а не отказываться)."""

    def __init__(self, replies=None, gate=None):
        self.replies = list(replies or [])
        self.calls = []
        self.gate = gate
        self.entered = threading.Event()
        self.released = threading.Event()

    def __call__(self):
        self.calls.append(1)
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
            self.released.set()
        return self.replies.pop(0) if self.replies else \
            {"outcome": "registered", "name": "pu-mop-1"}


TERMINAL = frozenset(opworker.TERMINAL)


def terminal(rec):
    return rec.get("status") if rec.get("status") in TERMINAL else None


def wait_terminal(api, op_id, who=ADMIN, timeout=10):
    """Дождаться терминала записи (поток исполнения -- асинхронный)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = api.result(op_id, who)
        if terminal(got):
            return got
        time.sleep(0.02)
    return api.result(op_id, who)


def main():
    c = Checks()
    ops = adminapi.Operations()
    opworker.register(ops)

    # ── приём: идемпотентность тем же payload -> тот же id, в памяти ─────
    # HYPOTHESIS: двойной клик обязан возвращать тот же id -- иначе второй
    # клик рождал бы вторую операцию на ту же цель; память и есть срок:
    # рестарт службы ниже честно отвечает unknown, а завершённая операция
    # ключ освобождает (после терминала тот же payload -- новая команда,
    # это проверяет tests/web_intents.py).
    held = threading.Event()
    keeper = Scripted(gate=held)
    api = adminapi.Api(ops, origin="https://pool.example", who=ADMIN,
                       executor=lambda action, target, params, who: keeper)
    first = api.submit("send", "pu-mop-1", {"message": "hi"}, ADMIN)
    second = api.submit("send", "pu-mop-1", {"message": "hi"}, ADMIN)
    c.check("#499 the same payload resubmits to the same id",
            first["id"] == second["id"], (first, second))
    # Другой payload на ту же цель -- вторая операция, не 409: исполнитель
    # ждёт семафор цели, а не отказывает (#497: 409 висел часами).
    other = api.submit("send", "pu-mop-1", {"message": "other"}, ADMIN)
    c.check("#499 a different payload on the same target is a new operation",
            other["id"] != first["id"], (first, other))
    held.set()
    wait_terminal(api, first["id"])

    # ── исполнение: поток на операцию, обе до терминала, вторая не 409 ──
    # HYPOTHESIS: две операции на одну цель подряд; прежний _active-замок
    # отказывал второй 409 «another operation is queued for this target»,
    # и отказ висел часами (#497). Семафор на цель ждёт: обе доводятся до
    # терминала последовательно, страница видит waiting.
    gate = threading.Event()
    slow = Scripted(gate=gate)
    fast = Scripted()
    api2 = adminapi.Api(ops, origin="https://pool.example", who=ADMIN,
                        executor=lambda action, target, params, who:
                        {"send:pu-mop-1:hi": slow,
                         "send:pu-mop-1:hi2": fast}[
                            f"{action}:{target}:{params.get('message')}"])
    r1 = api2.submit("send", "pu-mop-1", {"message": "hi"}, ADMIN)
    slow.entered.wait(5)
    r2 = api2.submit("send", "pu-mop-1", {"message": "hi2"}, ADMIN)
    c.check("#499 the second submission on a busy target is accepted, not 409",
            r2.get("id") and r2["id"] != r1["id"], (r1, r2))
    c.check("#499 while the first runs the second waits for the target",
            not fast.entered.wait(0.3), fast.calls)
    gate.set()
    a = wait_terminal(api2, r1["id"])
    b = wait_terminal(api2, r2["id"])
    c.check("#499 both operations on one target reach their terminal",
            terminal(a) == "succeeded" and terminal(b) == "succeeded", (a, b))
    c.check("#499 each effect fired exactly once, in order",
            len(slow.calls) == 1 and len(fast.calls) == 1 and slow.released.is_set(),
            (slow.calls, fast.calls))

    # ── ожидание видно статусом: waiting до семафора, running после ─────
    gate2 = threading.Event()
    w_slow = Scripted(gate=gate2)
    w_fast = Scripted()
    api_w = adminapi.Api(ops, origin="https://pool.example", who=ADMIN,
                         executor=lambda action, target, params, who:
                         {"a": w_slow, "b": w_fast}[params.get("message")])
    ra = api_w.submit("send", "pu-mop-1", {"message": "a"}, ADMIN)
    w_slow.entered.wait(5)
    rb = api_w.submit("send", "pu-mop-1", {"message": "b"}, ADMIN)
    c.check("#499 the waiting operation reads waiting",
            api_w.result(rb["id"], ADMIN).get("status") == "waiting",
            api_w.result(rb["id"], ADMIN))
    c.check("#499 the running operation reads running",
            api_w.result(ra["id"], ADMIN).get("status") == "running",
            api_w.result(ra["id"], ADMIN))
    gate2.set()
    wait_terminal(api_w, ra["id"])
    wait_terminal(api_w, rb["id"])

    # ── вердикт: SSE-кадр operation на терминальный статус ──────────────
    # Терминал публикуется в существующий поток /events (#424): кадр
    # operation несёт {id, action, target, status, error?} и не несёт
    # payload. Подписчик -- on_terminal адаптера; обвязка /events в
    # mop/cli/server/web.py зайдёт тем же крючком.
    api3 = adminapi.Api(ops, origin="https://pool.example", who=ADMIN)
    seen = []
    api3.on_terminal(lambda frame: seen.append(frame))
    eff = Scripted(replies=[{"outcome": "refused", "name": "pu-mop-1",
                             "error": "pu-mop-1: work in the clone"}])
    api3.executor = lambda action, target, params, who: eff
    api3.submit("send", "pu-mop-1", {"message": "hi"}, ADMIN)
    eff.entered.wait(5)
    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        time.sleep(0.02)
    c.check("#499 the terminal lands as an SSE operation frame",
            seen and seen[0].get("status") == "refused"
            and seen[0].get("action") == "send"
            and seen[0].get("target") == "pu-mop-1", seen)
    c.check("#499 the SSE frame carries no params payload",
            seen and "params" not in seen[0] and "message" not in seen[0], seen)

    # ── крах эффекта: вердикт failed, не вечный running (#497) ──────────
    # Прежняя гарантия жила журналом воркера (#476 «crash leaves a durable
    # failure»); с #499 крах эффекта ловит поток адаптера: запись -- failed
    # с причиной, ключ и семафор цели освобождены. #497 (таймаут
    # зависшего эффекта) -- отдельный слой; здесь только то, что упавший
    # эффект не оставляет «вечный running».
    class Boom:
        def __call__(self):
            raise RuntimeError("bus down")

    api_boom = adminapi.Api(ops, origin="https://pool.example", who=ADMIN,
                            executor=lambda *a: Boom())
    boom_frames = []
    api_boom.on_terminal(boom_frames.append)
    r = api_boom.submit("send", "pu-mop-2", {"message": "x"}, ADMIN)
    a = wait_terminal(api_boom, r["id"])
    c.check("#499 an effect that crashes reads failed, with a reason",
            a.get("status") == "failed" and a.get("error"), a)
    c.check("#499 the crashed operation frees its key for a resubmit",
            api_boom.submit("send", "pu-mop-2", {"message": "x"}, ADMIN)["id"]
            != r["id"])
    c.check("#499 the crash lands as an SSE frame, closed phrase only",
            boom_frames and boom_frames[0].get("status") == "failed"
            and boom_frames[0].get("error") == "operation failed", boom_frames)

    # ── сырая причина неуспеха -- в журнал службы (#506) ────────────────
    # HYPOTHESIS: после #499 (журнал ops.jsonl удалён) сырой текст
    # исключения жил только в памяти процесса: кадр /events и GET несут
    # закрытую фразу (#495), journalctl молчал, аудит пула -- только
    # {event: op}. Живой отказ («cannot read rugent: access denied» часами
    # читался как «operation failed») диагностировался воспроизведением
    # эффекта руками. SOLUTION: терминализация неуспеха пишет одну строку
    # в stderr службы (юнит не переопределяет StandardError -- она в
    # journalctl -u mop-web): op id, action, target, login, статус и
    # СЫРАЯ причина. Успех молчит (шум); публичный кадр не меняется --
    # #495 не ослабляется, строка живёт только в журнале оператора.
    # RESULT: первый прогон красный -- строки в stderr нет; STATUS:
    # FIXED -- see #506.
    class Deny:
        def __call__(self):
            raise RuntimeError("cannot read rugent: access denied")

    api_deny = adminapi.Api(ops, origin="https://pool.example", who=ADMIN,
                            executor=lambda *a: Deny())
    deny_frames = []
    api_deny.on_terminal(deny_frames.append)
    import io as _io
    import contextlib as _cl
    buf = _io.StringIO()
    with _cl.redirect_stderr(buf):
        r = api_deny.submit("send", "pu-rugent-1", {"message": "x"}, ADMIN)
        a = wait_terminal(api_deny, r["id"])
    line = buf.getvalue()
    c.check("#506 an unsuccessful terminal logs one line to the service journal",
            a.get("status") == "failed"
            and line.count("\n") == 1
            and r["id"] in line and "access denied" in line
            and "pu-rugent-1" in line, (a, line))
    c.check("#506 the journal line keeps the public frame closed",
            deny_frames and deny_frames[0].get("error") == "operation failed"
            and "access denied" not in json.dumps(deny_frames), deny_frames)
    # Успех молчит: succeeded-терминал не пишет в stderr ничего.
    api_ok = adminapi.Api(ops, origin="https://pool.example", who=ADMIN,
                          executor=lambda *a: Scripted())
    buf2 = _io.StringIO()
    with _cl.redirect_stderr(buf2):
        r = api_ok.submit("send", "pu-mop-3", {"message": "ok"}, ADMIN)
        wait_terminal(api_ok, r["id"])
    c.check("#506 a succeeded terminal stays silent in the journal",
            buf2.getvalue() == "", buf2.getvalue())

    # ── рестарт службы: неизвестные id -- «исход неизвестен, смотри журнал»
    # Дюрабельность «клик -> вердикт» через рестарт веба сознательно не
    # делаем: память и есть срок. Новый экземпляр незнакомый id отдаёт как
    # unknown со ссылкой на журнал, а не not found и не вечный accepted.
    api4 = adminapi.Api(ops, origin="https://pool.example", who=ADMIN,
                        executor=lambda *a: Scripted())
    got = api4.result(first["id"], ADMIN)
    c.check("#499 after a restart an unknown id reads unknown with a journal pointer",
            got.get("status") == "unknown"
            and "journal" in (got.get("error") or ""), got)

    # ── коды на двери: гейты адаптера живут как жили (#461) ─────────────
    ops_g = adminapi.Operations()
    opworker.register(ops_g)
    api_g = adminapi.Api(ops_g, origin="https://pool.example", who=ADMIN,
                         executor=lambda *a: Scripted())
    for who, want in ((None, 401), (("olga", "user"), 403)):
        h = Handler(who=who, body=req("send", "pu-mop-1", {"message": "hi"}), length=99)
        api_g.post(h)
        c.check(f"#461 {who and who[1] or 'guest'} -> {want}",
                h.sent and h.sent[0][0] == want, h.sent)

    def broken(*a, **k):
        raise RuntimeError("ldap socket: secret-path /etc/ld.pwd")
    h = Handler(body=req("send", "pu-mop-1", {"message": "hi"}), length=99)
    adminapi.Api(ops_g, origin="https://pool.example", who=ADMIN,
                 provider=broken, executor=lambda *a: Scripted()).post(h)
    c.check("#461 provider failure is closed",
            h.sent and h.sent[0][0] in (503, 401) and
            "secret" not in json.dumps(h.sent[0][1]), h.sent)
    for origin in (None, "https://evil.example"):
        h = Handler(origin=origin or "", body=req("send", "pu-mop-1"), length=99)
        api_g.post(h)
        c.check(f"#461 origin {origin!r} refused",
                h.sent and h.sent[0][0] == 403, h.sent)
    h = Handler(content_type="text/plain", body=b"x", length=1)
    api_g.post(h)
    c.check("#461 non-JSON content type refused", h.sent and h.sent[0][0] in (400, 415),
            h.sent)
    big = b"{" + b'"pad": "' + b"x" * (adminapi.BODY_MAX + 1) + b'"}'
    h = Handler(body=big, length=len(big))
    api_g.post(h)
    c.check("#461 oversized body refused", h.sent and h.sent[0][0] == 413, h.sent)
    h = Handler(body=req("send", "pu-mop-1")[:5], length=99)
    api_g.post(h)
    c.check("#461 truncated JSON refused", h.sent and h.sent[0][0] == 400, h.sent)
    h = Handler(body=b"not json", length=8)
    api_g.post(h)
    c.check("#461 garbage refused", h.sent and h.sent[0][0] == 400, h.sent)
    h = Handler(body=req("send", "pu-mop-1", actor="forged", env={"X": 1},
                         subject="mop.admin.cluster.rpc", verb="delete",
                         cwd="/", argv=["rm"]), length=None)
    h.headers["Content-Length"] = str(len(h.body))
    api_g.post(h)
    c.check("#461 forged actor/env/subject/verb/cwd/argv refused as unknown fields",
            h.sent and h.sent[0][0] == 400, h.sent)

    # ── реестр: незарегистрированное -- unsupported, не 500 ─────────────
    bare = adminapi.Operations()
    api_bare = adminapi.Api(bare, origin="https://pool.example", who=ADMIN,
                            executor=lambda *a: Scripted())
    h = Handler(body=req("restart", "pu-mop-1"), length=None)
    h.headers["Content-Length"] = str(len(h.body))
    api_bare.post(h)
    c.check("#461 unregistered operation is 404/unsupported, no button",
            h.sent and h.sent[0][0] == 404
            and h.sent[0][1].get("error") == "unsupported operation", h.sent)
    for verb in ("add", "update", "restart", "recycle", "delete", "wipe", "model"):
        body = req(verb, target="pu-mop-1")
        h = Handler(who=ADMIN, body=body, length=len(body))
        api_g.post(h)
        c.check(f"#489 opworker.register wires {verb}",
                h.sent and h.sent[0][0] == 202 and h.sent[0][1].get("id"), h.sent)
    h = Handler(body=req("send", "pu-mop-1", params={"message": "hi",
                                                     "notify_when_idle": True,
                                                     "wait": 60}), length=None)
    h.headers["Content-Length"] = str(len(h.body))
    api_g.post(h)
    c.check("#461 first-release send policy: notify/wait are not accepted",
            h.sent and h.sent[0][0] == 400, h.sent)

    # ── GET результата: admin-only, no payload, статусы ─────────────────
    got = api.result(first["id"], ADMIN)
    c.check("#461 own result is readable", got.get("id") == first["id"], got)
    c.check("#461 a result of an unknown id reads unknown with the journal pointer",
            api.result("no-such-id", ADMIN).get("status") == "unknown", None)
    c.check("#461 a result is admin-only",
            api.result(first["id"], USER).get("status") in ("401", "403", "refused"))
    c.check("#461 the result carries no message payload",
            "message" not in json.dumps(got), got)
    c.check("#461 results never ride the public snapshot",
            not any(k in got for k in ("params", "message")), got)

    # ── санитизация деталей ─────────────────────────────────────────────
    c.expect("#461 sanitized error keeps the words, drops the path",
             adminapi.sanitize(RuntimeError("boom: /home/pool/secret.key gone")),
             "boom gone")
    c.expect("#461 sanitize keeps a multiword line without paths",
             adminapi.sanitize(ValueError("target pu-mop-1 refused: held by olga")),
             "target pu-mop-1 refused held by olga")

    # ── параллельные контексты: два запроса -- две операции ─────────────
    fresh = adminapi.Api(ops, origin="https://pool.example",
                         executor=lambda *a: Scripted())
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
    got = adminapi.project_list(registry(["git@h:g/mop.git", "junk line"]))
    c.check("#472 a non-origin line is dropped, the rest survives",
            [p["name"] for p in got] == ["mop"], got)

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

    listing_api = adminapi.Api(ops, origin="https://pool.example", who=ADMIN,
                               executor=lambda *a: Scripted())
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

    def broken_provider(*a, **k):
        raise RuntimeError("ldap socket /etc/ld.pwd down")
    closed = adminapi.Api(ops, origin="https://pool.example",
                          provider=broken_provider, executor=lambda *a: Scripted())
    h = ListHandler(ADMIN)
    closed.projects(h, registry(["git@h:g/mop.git"]))
    c.check("#472 GET projects with a dead provider is closed, no raw detail",
            h.sent and h.sent[0][0] in (503, 401)
            and "ldap" not in json.dumps(h.sent[0][1]), h.sent)

    # ── add/update в реестре операций (#470) ────────────────────────────
    wired = adminapi.Operations()
    opworker.register(wired)
    wired_api = adminapi.Api(wired, origin="https://pool.example", who=ADMIN,
                             executor=lambda *a: Scripted())

    def post(action, target="pu-mop-1", params=None):
        body = req(action, target=target, params=params or {})
        h = Handler(body=body, length=len(body))
        wired_api.post(h)
        return h

    h = post("add", target=None, params={"origin": "git@h:g/mop.git"})
    c.check("#470 add without a target is accepted (202)",
            h.sent and h.sent[0][0] == 202 and h.sent[0][1].get("id"), h.sent)
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
    h = post("restart", target=None, params={})
    c.check("#470 a lifecycle op still requires its target",
            h.sent and h.sent[0][0] == 400, h.sent)
    h = post("update", target=None, params={"fresh": False})
    c.check("#470 update without a target is refused",
            h.sent and h.sent[0][0] == 400, h.sent)

    # ── recycle/delete/wipe в реестре (#471) ────────────────────────────
    unwired = adminapi.Operations()
    unwired_api = adminapi.Api(unwired, origin="https://pool.example", who=ADMIN,
                               executor=lambda *a: Scripted())

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
    for verb in ("recycle", "delete", "wipe"):
        h = post(verb, params={"force": True})
        c.check(f"#471 {verb} refuses any parameter (force never rides)",
                h.sent and h.sent[0][0] == 400
                and "unknown parameters" in h.sent[0][1].get("error", ""),
                h.sent)

    # ── каталог моделей для формы (#468) ────────────────────────────────
    from mop.common import llm

    c.check("#468 the catalog parser is the client one, not a copy",
            llm.catalog_ids({"data": [{"id": "glm-5.3"}]}) == ["glm-5.3"], None)
    try:
        llm.catalog_ids({"data": [{"name": "opus"}]})
        c.check("#468 a malformed catalog is rejected by the shared parser", False)
    except ValueError:
        c.check("#468 a malformed catalog is rejected by the shared parser", True)

    def catalog(readings):
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

    listing_api = adminapi.Api(ops, origin="https://pool.example", who=ADMIN,
                               executor=lambda *a: Scripted())
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
