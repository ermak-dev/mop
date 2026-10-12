#!/usr/bin/env python3
"""Аудит завершения веб-операции: production-путь на подменённой шине.

HYPOTHESIS (#575): терминал операции публиковался одним публичным кадром
SSE {id, action, target, status, error?} без login -- и его же читал
внутренний аудит: принципал брался из отсутствующего поля кадра (вечно
None), проект передавался литеральным None, ops.record не нёс ни id
операции, ни терминального исхода. Журнал шины не связывал действие с
HTTP-принципалом и не отличал успех от отказа; #506 печатало сырую
причину в stderr, но аудит не чинило.
SOLUTION: внутреннее событие завершения в адаптере (on_completion) --
проекция, ОТДЕЛЬНАЯ от публичного кадра: проверенный login из записи
приёма, id операции, терминальный исход, имя из доверенного вердикта
(add называет созданного папета, а не входную targetless просьбу) и
проект из подтверждённого контекста эффекта -- факт вердикта (спека
цели/Meta.origin, у add -- зарегистрированный origin), не префикс имени
и не payload. Аудит едет на шину ops.record'ом с op/status и темой
проекта записи; сетевой прогулки после эффекта нет, неизвестный проект
остаётся честным None. Публичный кадр не ширеет: login и проект в него
не попадают.
RESULT: успех/отказ/unknown/add/send проверены на настоящем ADMINAPI
(глобальная регистрация колбэков) с подменой только шины; утечки
payload и сырых ошибок -- в обе проекции. До фикса каждый принципал/
op/status/project -- красный.
"""
import json
import os
import sys
import threading
import time

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import bus, puppets  # noqa: E402
from mop.server import opworker  # noqa: E402
from mop.cli.server import web as webmod  # noqa: E402

ADMIN = ("anton", "admin")
ORIGIN = "git@example.test:g/actual.git"
TARGET_PROJECT = puppets.project_of(ORIGIN)   # проект решает origin, не префикс


def settled(api, op_id, timeout=10):
    """Дождаться терминала записи адаптера (поток -- асинхронный)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = api.result(op_id, ADMIN)
        if got.get("status") in opworker.TERMINAL:
            return got
        time.sleep(0.02)
    return api.result(op_id, ADMIN)


def wait_audit(emits, op_id, timeout=10):
    """Дождаться аудита конкретной операции: терминал записи и emit --
    два факта одного потока, а список уже несёт аудиты прошлых сценариев
    -- ждать надо СВОЮ запись, а не первый элемент."""
    deadline = time.monotonic() + timeout
    while audit_of(emits, op_id)[0] is None and time.monotonic() < deadline:
        time.sleep(0.02)
    return emits


def audit_of(emits, op_id):
    """Запись аудита операции: (fields, subject) | (None, None)."""
    for fields, subject in emits:
        if fields.get("op") == op_id:
            return fields, subject
    return None, None


def fake_bus(emits, restarts=None, clone_dirty=0, clone_owner=None,
             spec_origin=ORIGIN, block_restart=None, add_name="pu-aud-add-1"):
    """Подмена ТОЛЬКО шины: глаголы сервиса кластера, узла и публикация.

    Как настоящей службе: spec/alloc/projects/roster -- чтения, restart --
    мутация со сдвигом счётчика (наблюдение #464 видит сигнал сразу),
    add -- доверенный ответ с именем созданного папета. block_restart
    держит эффект рестарта до отпуска -- сценарий unknown (#497/#569)."""
    if restarts is None:
        restarts = [7]
    webmod._ROSTER_CACHE[0] = 0.0      # кэш резолвера -- модульный
    webmod._ROSTER_CACHE[1] = {}

    def call_cluster(verb, project=None, **fields):
        if verb == "spec":
            # spec_origin=False -- спека без origin: проект не разрешим
            return {"meta": {} if spec_origin is False
                    else {"origin": spec_origin}}
        if verb == "alloc":
            return {"alloc": {"ID": "a1", "JobID": fields.get("name"),
                              "NodeName": "node-a",
                              "ClientStatus": "running",
                              "task": {"state": "running",
                                       "restarts": restarts[0],
                                       "exit": None, "next_s": None,
                                       "failed": False}}}
        if verb == "restart":
            if block_restart is not None:
                block_restart.wait(10)
            restarts[0] += 1
            return {"ok": True}
        if verb == "add":
            return {"ok": True, "name": add_name}
        if verb == "projects":
            return {"names": [TARGET_PROJECT]}
        if verb == "roster":
            return {"items": []}       # кандидат резолвера -- None: проект
            # подтверждает спека (#566, раунд 3), не ростер
        return {}

    def request(node, verb, **fields):
        if verb == "state":
            return {"present": True, "session": "idle 1 1",
                    "clone": {"cur": "master", "def": "master",
                              "dirty": clone_dirty, "ahead": 0,
                              "owner": clone_owner}}
        if verb == "send":
            return {"msg_id": "message-1"}
        raise AssertionError(verb)

    def emit(fields, project=None):
        emits.append((dict(fields or {}), project))

    return patched(bus, call_cluster=call_cluster, request=request,
                   emit=emit)


def main():
    c = Checks()
    emits, frames = [], []
    # Публичная проекция -- подписчиком, как /events: кадр в список.
    webmod.ADMINAPI.on_terminal(frames.append)

    # ── успех: рестарт чистой цели -- принципал, id, исход, проект ──────
    with fake_bus(emits):
        op = webmod.ADMINAPI.submit("restart", "pu-aud-1",
                                    {"note": "audit-secret-1"}, ADMIN)
        got = settled(webmod.ADMINAPI, op["id"])
        wait_audit(emits, op["id"])
    rec, subj = audit_of(emits, op["id"])
    c.check("#575 a succeeded restart audits the verified principal",
            got.get("status") == "succeeded" and rec is not None
            and rec.get("event") == "op" and rec.get("verb") == "restart"
            and rec.get("principal") == "anton"
            and rec.get("sender") == "mop-web", (got, rec))
    c.check("#575 the audit carries the operation id and terminal status",
            rec is not None and rec.get("op") == op["id"]
            and rec.get("status") == "succeeded", (op, rec))
    c.check("#575 the audit project is confirmed and the subject matches",
            rec is not None and rec.get("name") == "pu-aud-1"
            and rec.get("project") == TARGET_PROJECT
            and subj == TARGET_PROJECT, (rec, subj))
    frame = next((f for f in frames if f.get("id") == op["id"]), None)
    c.check("#575 the public SSE frame stays narrow",
            frame is not None and frame.get("status") == "succeeded"
            and not any(k in frame for k in ("login", "principal",
                                             "project", "params")), frame)

    # ── отказ после подтверждения: проект известен и в отказе ──────────
    with fake_bus(emits, clone_dirty=3):
        op2 = webmod.ADMINAPI.submit("restart", "pu-aud-2", {}, ADMIN)
        got2 = settled(webmod.ADMINAPI, op2["id"])
        wait_audit(emits, op2["id"])
    rec2, _ = audit_of(emits, op2["id"])
    c.check("#575 a refused restart audits refusal with the known project",
            got2.get("status") == "refused" and rec2 is not None
            and rec2.get("principal") == "anton"
            and rec2.get("status") == "refused"
            and rec2.get("op") == op2["id"]
            and rec2.get("project") == TARGET_PROJECT, (got2, rec2))

    # ── ранний отказ до разрешения проекта: честный None ────────────────
    with fake_bus(emits, spec_origin=False):
        op3 = webmod.ADMINAPI.submit("restart", "pu-aud-3", {}, ADMIN)
        got3 = settled(webmod.ADMINAPI, op3["id"])
        wait_audit(emits, op3["id"])
    rec3, subj3 = audit_of(emits, op3["id"])
    c.check("#575 an early refusal audits an honest unknown project",
            got3.get("status") == "refused" and rec3 is not None
            and rec3.get("principal") == "anton"
            and rec3.get("status") == "refused"
            and rec3.get("op") == op3["id"]
            and rec3.get("project") is None
            and subj3 is None, (got3, rec3, subj3))

    # ── send: проект из origin спеки, принципал -- HTTP ────────────────
    with fake_bus(emits), \
            patched(puppets,
                    _cluster=lambda verb, **kw: {"meta": {"origin": ORIGIN}},
                    node_of=lambda name: "node-a"):
        op4 = webmod.ADMINAPI.submit("send", "pu-aud-4",
                                     {"message": "body-secret-1"}, ADMIN)
        got4 = settled(webmod.ADMINAPI, op4["id"])
        wait_audit(emits, op4["id"])
    rec4, _ = audit_of(emits, op4["id"])
    c.check("#575 a send audits the spec-origin project",
            got4.get("status") == "succeeded" and rec4 is not None
            and rec4.get("principal") == "anton"
            and rec4.get("op") == op4["id"]
            and rec4.get("status") == "succeeded"
            and rec4.get("project") == TARGET_PROJECT, (got4, rec4))

    # ── unknown: ответ ушёл по сроку, аудит несёт unknown и проект ──────
    blocker = threading.Event()
    fast = opworker.Worker(effect_timeout=0.5, observe_wait=0,
                           sleep=lambda s: time.sleep(0.02),
                           registry=lambda: {ORIGIN},
                           workspace=lambda origin: ("", None))
    with patched(webmod, WORKER=fast), \
            fake_bus(emits, block_restart=blocker):
        op5 = webmod.ADMINAPI.submit("restart", "pu-aud-5", {}, ADMIN)
        got5 = settled(webmod.ADMINAPI, op5["id"])
        wait_audit(emits, op5["id"])
        blocker.set()   # отпустить хвост эффекта ВНУТРИ подмены шины
    rec5, _ = audit_of(emits, op5["id"])
    c.check("#575 an unknown outcome audits unknown with principal and project",
            got5.get("status") == "unknown" and rec5 is not None
            and rec5.get("principal") == "anton"
            and rec5.get("status") == "unknown"
            and rec5.get("op") == op5["id"]
            and rec5.get("project") == TARGET_PROJECT, (got5, rec5))
    # ── add: имя из доверенного ответа, проект -- из origin ────────────
    fast_add = opworker.Worker(effect_timeout=5, observe_wait=0,
                               sleep=lambda s: time.sleep(0.02),
                               registry=lambda: {ORIGIN},
                               workspace=lambda origin: ("", None))
    with patched(webmod, WORKER=fast_add), \
            fake_bus(emits, add_name="pu-aud-made-1"):
        op6 = webmod.ADMINAPI.submit("add", None, {"origin": ORIGIN}, ADMIN)
        got6 = settled(webmod.ADMINAPI, op6["id"])
        wait_audit(emits, op6["id"])
    rec6, subj6 = audit_of(emits, op6["id"])
    c.check("#575 add audits the name from the trusted result",
            got6.get("status") == "succeeded" and rec6 is not None
            and rec6.get("name") == "pu-aud-made-1"
            and rec6.get("project") == TARGET_PROJECT
            and rec6.get("principal") == "anton"
            and rec6.get("op") == op6["id"]
            and subj6 == TARGET_PROJECT, (got6, rec6, subj6))

    # ── утечки: payload и сырые ошибки не едут ни в аудит, ни в кадры ──
    blob_audit = json.dumps([f for f, _ in emits], ensure_ascii=False)
    blob_frames = json.dumps(frames, ensure_ascii=False)
    c.check("#575 no payload or raw error reaches the audit or the frames",
            not any(mark in blob for mark in ("audit-secret-1",
                                              "body-secret-1",
                                              "work in the clone")
                    for blob in (blob_audit, blob_frames))
            and all("error" not in f for f, _ in emits),
            (blob_audit, blob_frames))
    return c.report("web_audit")


if __name__ == "__main__":
    sys.exit(main())
