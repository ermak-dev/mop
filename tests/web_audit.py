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
STATUS: FIXED — see #575.
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
             spec_origin=ORIGIN, block_restart=None, add_name="pu-aud-add-1",
             state_error=None, block_alloc=None, block_send=None,
             block_place=None):
    """Подмена ТОЛЬКО шины: глаголы сервиса кластера, узла и публикация.

    Как настоящей службе: spec/alloc/projects/roster -- чтения, restart --
    мутация со сдвигом счётчика (наблюдение #464 видит сигнал сразу),
    add -- доверенный ответ с именем созданного папета. block_restart
    держит эффект рестарта до отпуска -- сценарий unknown (#497/#569);
    block_alloc держит ПРЕФЕТЧ после подтверждённой спеки (ревью 2 #575),
    state_error бросает узловой state ПОСЛЕ подтверждения спеки."""
    if restarts is None:
        restarts = [7]
    registered = []                    # выданные add-имена (block_place)
    webmod._ROSTER_CACHE[0] = 0.0      # кэш резолвера -- модульный
    webmod._ROSTER_CACHE[1] = {}

    def call_cluster(verb, project=None, **fields):
        if verb == "spec":
            # spec_origin=False -- спека без origin: проект не разрешим
            return {"meta": {} if spec_origin is False
                    else {"origin": spec_origin}}
        if verb == "alloc":
            if block_alloc is not None:
                block_alloc.wait(10)
            if block_place is not None and registered:
                # Ответ регистрации уже дал доверенное имя; ПОСЛЕ него
                # placement висит -- ревью 3 #575 (timeout у add).
                block_place.wait(10)
                return {}
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
            registered.append(add_name)
            return {"ok": True, "name": add_name}
        if verb == "projects":
            return {"names": [TARGET_PROJECT]}
        if verb == "roster":
            return {"items": []}       # кандидат резолвера -- None: проект
            # подтверждает спека (#566, раунд 3), не ростер
        return {}

    def request(node, verb, **fields):
        if verb == "state":
            if state_error is not None:
                raise state_error
            return {"present": True, "session": "idle 1 1",
                    "clone": {"cur": "master", "def": "master",
                              "dirty": clone_dirty, "ahead": 0,
                              "owner": clone_owner}}
        if verb == "send":
            if block_send is not None:
                # Спека уже подтвердила проект, доставка висит -- ревью 3
                # #575 (timeout у send).
                block_send.wait(10)
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
            and subj3 == bus.ADMIN, (got3, rec3, subj3))

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

    # ── ревью 2 #575: контекст -- в момент факта, не после возврата ────
    # HYPOTHESIS: имя и проект аудита брались из вердикта воркера, а
    # вердикта нет у gate-отказов ({"status": "refused"} без name),
    # у зависшего префетча (unknown до возврата prepare) и у краха
    # эффекта; подтверждённый спекой проект умирал на ЛЮБОЙ позднейшей
    # ошибке префетча, отказ реестра add выдумывал проект из
    # незарегистрированного origin. SOLUTION: одно внутреннее completion
    # из записи приёма (login, target) и подтверждённого контекста
    # (проект фиксируется В МОМЕНТ подтверждения спекой, переживёт и
    # отказ, и зависание); публичный кадр -- проекция из него; тема
    # неизвестного проекта -- явная нейтральная admin.
    # RESULT: имя цели живёт у refused/failed/unknown, проект -- после
    # BusError узла и зависшего alloc, add незарегистрированного origin
    # без проекта в нейтральной теме, крах после префетча не теряет
    # контекст; запись проходит реальный full_entry.

    # 1) gate-отказ (dirty): имя цели -- из записи приёма
    with fake_bus(emits, clone_dirty=3):
        op7 = webmod.ADMINAPI.submit("restart", "pu-aud-21", {}, ADMIN)
        got7 = settled(webmod.ADMINAPI, op7["id"])
        wait_audit(emits, op7["id"])
    rec7, subj7 = audit_of(emits, op7["id"])
    c.check("#575 a gate refusal keeps the target name in the audit",
            got7.get("status") == "refused" and rec7 is not None
            and rec7.get("name") == "pu-aud-21"
            and rec7.get("status") == "refused"
            and rec7.get("project") == TARGET_PROJECT
            and subj7 == TARGET_PROJECT, (got7, rec7, subj7))

    # 2) узел упал ПОСЛЕ подтверждения спеки: проект жив
    with fake_bus(emits, state_error=bus.BusError("node down")):
        op8 = webmod.ADMINAPI.submit("restart", "pu-aud-22", {}, ADMIN)
        got8 = settled(webmod.ADMINAPI, op8["id"])
        wait_audit(emits, op8["id"])
    rec8, _ = audit_of(emits, op8["id"])
    c.check("#575 a node error after the spec keeps the confirmed project",
            got8.get("status") == "refused" and rec8 is not None
            and rec8.get("project") == TARGET_PROJECT
            and rec8.get("name") == "pu-aud-22", (got8, rec8))

    # 3) add незарегистрированного origin: реестр отверг -- проекта нет
    fast_refuse = opworker.Worker(effect_timeout=5, observe_wait=0,
                                  sleep=lambda s: time.sleep(0.02),
                                  registry=lambda: set(),
                                  workspace=lambda origin: ("", None))
    with patched(webmod, WORKER=fast_refuse), \
            fake_bus(emits):
        op9 = webmod.ADMINAPI.submit(
            "add", None, {"origin": "git@example.test:g/unregistered.git"},
            ADMIN)
        got9 = settled(webmod.ADMINAPI, op9["id"])
        wait_audit(emits, op9["id"])
    rec9, subj9 = audit_of(emits, op9["id"])
    c.check("#575 a refused registry gate invents no project",
            got9.get("status") == "refused" and rec9 is not None
            and rec9.get("project") is None
            and rec9.get("name") is None
            and subj9 == bus.ADMIN, (got9, rec9, subj9))

    # 4) спека подтверждена, alloc висит до unknown: контекст жив
    alloc_gate = threading.Event()
    fast2 = opworker.Worker(effect_timeout=0.5, observe_wait=0,
                            sleep=lambda s: time.sleep(0.02),
                            registry=lambda: {ORIGIN},
                            workspace=lambda origin: ("", None))
    with patched(webmod, WORKER=fast2), \
            fake_bus(emits, block_alloc=alloc_gate):
        op10 = webmod.ADMINAPI.submit("restart", "pu-aud-23", {}, ADMIN)
        got10 = settled(webmod.ADMINAPI, op10["id"])
        wait_audit(emits, op10["id"])
        alloc_gate.set()   # отпустить префетч ВНУТРИ подмены шины
    rec10, _ = audit_of(emits, op10["id"])
    c.check("#575 a hung prefetch after the spec keeps name and project",
            got10.get("status") == "unknown" and rec10 is not None
            and rec10.get("project") == TARGET_PROJECT
            and rec10.get("name") == "pu-aud-23"
            and rec10.get("status") == "unknown", (got10, rec10))

    # 5) крах эффекта ПОСЛЕ успешного префетча: контекст не исчезает
    class Exploding:
        """Воркер, чей эффект падает после подтверждённого префетча."""

        def run(self, verb, target, params, login, prepare=None, **kw):
            prepare()
            raise RuntimeError("effect blew up")

    with patched(webmod, WORKER=Exploding()), \
            fake_bus(emits):
        op11 = webmod.ADMINAPI.submit("restart", "pu-aud-24", {}, ADMIN)
        got11 = settled(webmod.ADMINAPI, op11["id"])
        wait_audit(emits, op11["id"])
    rec11, _ = audit_of(emits, op11["id"])
    c.check("#575 an effect crash after the preflight keeps the context",
            got11.get("status") == "failed" and rec11 is not None
            and rec11.get("project") == TARGET_PROJECT
            and rec11.get("name") == "pu-aud-24"
            and rec11.get("status") == "failed", (got11, rec11))

    # 6) запись аудита проходит реальный full_entry журнала
    from mop.server import web as srvweb
    entry = srvweb.full_entry({**(rec7 or {}), "at": time.time()})
    c.check("#575 the audit record survives the real journal entry",
            entry.get("op") == op7["id"]
            and entry.get("status") == "refused"
            and entry.get("principal") == "anton"
            and entry.get("project") == TARGET_PROJECT, entry)

    # ── ревью 3 #575: контекст send/add -- в момент факта, не по вердикту ──
    # HYPOTHESIS: send/add заполняли контекст аудита только после
    # возврата Worker.run -- timeout (#497/#569) съедал уже полученные
    # факты: у send спека подтвердила проект ДО зависшей доставки, у add
    # реестр подтвердил origin и RPC уже вернул доверенное имя ДО
    # зависшего placement, а аудит читал None/None.
    # SOLUTION: тот же per-op контекст дотянуть внутрь эффектов send/add:
    # проект фиксируется при подтверждении спеки/реестра, созданное имя --
    # при доверенном ответе регистрации; вердикт по-прежнему только
    # заполняет пустое. Контекст живёт в замыкании фабрики (не в общем
    # поле WORKER), параллельные операции его не смешивают.
    # RESULT: timeout-исходы несут имя/проект; параллельные операции
    # разных проектов не путают контексты; публичная проекция узкая.

    # 1) send: спека подтверждена, доставка висит до unknown
    send_gate = threading.Event()
    fast3 = opworker.Worker(effect_timeout=0.5, observe_wait=0,
                            sleep=lambda s: time.sleep(0.02),
                            registry=lambda: {ORIGIN},
                            workspace=lambda origin: ("", None))
    with patched(webmod, WORKER=fast3), \
            fake_bus(emits, block_send=send_gate), \
            patched(puppets,
                    _cluster=lambda verb, **kw: {"meta": {"origin": ORIGIN}},
                    node_of=lambda name: "node-a"):
        op12 = webmod.ADMINAPI.submit("send", "pu-aud-31",
                                      {"message": "hung-send"}, ADMIN)
        got12 = settled(webmod.ADMINAPI, op12["id"])
        wait_audit(emits, op12["id"])
        send_gate.set()   # отпустить доставку ВНУТРИ подмены шины
    rec12, _ = audit_of(emits, op12["id"])
    c.check("#575 a hung send after the spec keeps name and project",
            got12.get("status") == "unknown" and rec12 is not None
            and rec12.get("name") == "pu-aud-31"
            and rec12.get("project") == TARGET_PROJECT
            and rec12.get("status") == "unknown", (got12, rec12))

    # 2) add: имя получено, placement висит до unknown
    place_gate = threading.Event()
    fast4 = opworker.Worker(effect_timeout=0.5, observe_wait=0,
                            sleep=lambda s: time.sleep(0.02),
                            registry=lambda: {ORIGIN},
                            workspace=lambda origin: ("", None))
    with patched(webmod, WORKER=fast4), \
            fake_bus(emits, block_place=place_gate,
                     add_name="pu-aud-made-32"):
        op13 = webmod.ADMINAPI.submit("add", None, {"origin": ORIGIN}, ADMIN)
        got13 = settled(webmod.ADMINAPI, op13["id"])
        wait_audit(emits, op13["id"])
        place_gate.set()   # отпустить placement ВНУТРИ подмены шины
    rec13, _ = audit_of(emits, op13["id"])
    c.check("#575 a hung add after the name keeps name and project",
            got13.get("server") if False else got13.get("status") == "unknown"
            and rec13 is not None
            and rec13.get("name") == "pu-aud-made-32"
            and rec13.get("project") == TARGET_PROJECT
            and rec13.get("status") == "unknown", (got13, rec13))

    # 3) параллельные операции разных проектов: контексты не смешиваются
    OTHER = "git@example.test:g/other.git"
    OTHER_PROJECT = puppets.project_of(OTHER)
    swap = {"origin": ORIGIN}   # спека отвечает по имени цели

    def spec_swap(verb, project=None, **fields):
        if verb == "spec":
            return {"meta": {"origin": swap["origin"]}}
        return call_cluster_audit(verb, project, **fields)
    call_cluster_audit = None  # заполняется ниже после fake_bus

    par_emits = []
    fast5 = opworker.Worker(effect_timeout=5, observe_wait=0,
                            sleep=lambda s: time.sleep(0.02),
                            registry=lambda: {ORIGIN, OTHER},
                            workspace=lambda origin: ("", None))

    def par_call_cluster(verb, project=None, **fields):
        if verb == "spec":
            # целевая спека отвечает origin своей цели (по name)
            named = fields.get("name")
            origin = ORIGIN if named == "pu-par-1" else OTHER
            return {"meta": {"origin": origin}}
        if verb == "alloc":
            return {"alloc": {"ID": "a1", "JobID": fields.get("name"),
                              "NodeName": "node-a", "ClientStatus": "running",
                              "task": {"state": "running", "restarts": 7,
                                       "exit": None, "next_s": None,
                                       "failed": False}}}
        if verb == "restart":
            return {"ok": True}
        if verb == "projects":
            return {"names": [TARGET_PROJECT, OTHER_PROJECT]}
        if verb == "roster":
            return {"items": []}
        return {}

    def par_request(node, verb, **fields):
        if verb == "state":
            return {"present": True, "session": "idle 1 1",
                    "clone": {"cur": "master", "def": "master",
                              "dirty": 0, "ahead": 0, "owner": None}}
        raise AssertionError(verb)

    def par_emit(fields, project=None):
        par_emits.append((dict(fields or {}), project))

    with patched(webmod, WORKER=fast5), \
            patched(bus, call_cluster=par_call_cluster,
                    request=par_request, emit=par_emit):
        a = webmod.ADMINAPI.submit("restart", "pu-par-1", {}, ADMIN)
        b = webmod.ADMINAPI.submit("restart", "pu-par-2", {}, ADMIN)
        wait_audit(par_emits, a["id"])
        wait_audit(par_emits, b["id"])
    ra, sa = audit_of(par_emits, a["id"])
    rb, sb = audit_of(par_emits, b["id"])
    c.check("#575 parallel ops of different projects keep their contexts",
            ra is not None and rb is not None
            and ra.get("project") == TARGET_PROJECT
            and rb.get("project") == OTHER_PROJECT
            and sa == TARGET_PROJECT and sb == OTHER_PROJECT,
            (ra, rb, sa, sb))

    # ── утечки: payload и сырые ошибки не едут ни в аудит, ни в кадры ──
    blob_audit = json.dumps([f for f, _ in emits], ensure_ascii=False)
    blob_frames = json.dumps(frames, ensure_ascii=False)
    c.check("#575 no payload or raw error reaches the audit or the frames",
            not any(mark in blob for mark in ("audit-secret-1",
                                              "body-secret-1",
                                              "work in the clone",
                                              "unregistered",
                                              "node down")
                    for blob in (blob_audit, blob_frames))
            and all("error" not in f for f, _ in emits),
            (blob_audit, blob_frames))
    return c.report("web_audit")


if __name__ == "__main__":
    sys.exit(main())
