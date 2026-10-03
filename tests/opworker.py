#!/usr/bin/env python3
"""Исполнитель веб-операций без пула: python3 tests/opworker.py

Воркер #476 исполняет операции дашборда через библиотечный workflow #458
под инвариантом #460. Его собственная обязанность -- устойчивость: операция,
начатая кликом, обязана пережить крах службы с честным исходом, а повтор
клика не должен запускать её дважды. Всё это -- чистые механизмы над
каталогом, и проверяются здесь.

Это не фреймворк: транспорт подменяется, эффекты считаются вызовами.
"""
import json
import os
import sys
import tempfile
import threading

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import ops  # noqa: E402
from mop.server import opworker  # noqa: E402


# ── каркас проверки ──────────────────────────────────────────────────────
class Recorder:
    """Транспорт-подмена: глаголы #458 считаются, любой может упасть.

    alloc -- read-глагол наблюдения (#464): отдаёт неизменную сводку задачи;
    счётчик рестартов стоит -- наблюдение честно кончается unknown, а сам
    эффект считается по рестарт-RPC."""
    ALLOC = {"ID": "a1", "ClientStatus": "running",
             "task": {"state": "running", "restarts": 7, "exit": None,
                      "next_s": None, "failed": False}}

    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at or {}
        self.answers = {"add": {"ok": True, "name": "pu-mop-9"},
                        "alloc": {"alloc": dict(self.ALLOC)}}

    def __call__(self, verb, **fields):
        self.calls.append((verb, fields))
        why = self.fail_at.get(verb) or self.fail_at.get("*")
        if why:
            raise ops.bus.Refused(why) if "refuse" in str(why) else RuntimeError(why)
        got = self.answers.get(verb)
        if got is None:
            return {}
        return dict(got) if isinstance(got, dict) else got

    def effect_rpcs(self):
        """Вызовы-эффекты без read-глаголов наблюдения (#464)."""
        return [call for call in self.calls
                if not (isinstance(call, tuple) and len(call) == 2
                        and call[0] in ("alloc",))]


def one(now=(lambda: 1_000.0)):
    """Воркер на свежем временном каталоге.

    Наблюдение рестарта (#464): нулевой срок и переданный сон -- иначе
    каждый рестарт матрицы опрашивал бы свои observe_wait по-настоящему."""
    root = tempfile.mkdtemp(prefix="mop-test-476-")
    return opworker.Worker(root, now=now, observe_wait=0,
                           sleep=lambda s: None), root


def facts_clean():
    """Факты, при которых safety говорит «можно»."""
    return {"clone": {"cur": "master", "def": "master", "dirty": 0, "ahead": 0,
                      "owner": {"user": "anton", "at": 999_995}}}


def payload_of(rec):
    """Payload исполнителя из каталога: файл операции."""
    return rec


def main():
    c = Checks()
    what = "#476 "

    # ── приём: дедупликация и.intent до эффекта ─────────────────────────
    # HYPOTHESIS: повторный клик того же человека с тем же ключом -- тот же
    # запрос: без дедупликации двойной клик запускал бы операцию дважды, а
    # крах между приёмом и эффектом оставлял бы «что-то случилось, но что?»
    # SOLUTION: приём транзакционен -- intent пишется ДО эффекта и несёт
    # actor, ключ и хэш нормализованного запроса; тот же ключ с другим
    # payload -- отказ, а не тихий второй запуск.
    w, root = one()
    transport = Recorder()
    out1 = w.submit("anton", "restart", "pu-mop-1", {"force": False},
                    transport=transport, key="click-1",
                    safety=lambda name, project, **kw: (True, None))
    out2 = w.submit("anton", "restart", "pu-mop-1", {"force": False},
                    transport=transport, key="click-1",
                    safety=lambda name, project, **kw: (True, None))
    c.check(what + "a duplicate click returns the same operation",
            out1.get("op") == out2.get("op"), (out1, out2))
    c.check(what + "the duplicate fires the effect once",
            len(transport.effect_rpcs()) == 1, transport.calls)
    # Ключ тот же, payload другой -- отказ, ключ не даётся бесплатно.
    other = w.submit("anton", "restart", "pu-mop-1", {"force": True},
                     transport=transport, key="click-1",
                     safety=lambda name, project, **kw: (True, None))
    c.check(what + "the same key with another payload is refused",
            other.get("status") == "refused", other)

    # ── переходы: queued -> running -> исход ────────────────────────────
    c.check(what + "the submission reaches a terminal status through the stages",
            out1.get("status") in opworker.TERMINAL, out1)
    c.check(what + "the stages are structural (queued, running, then the outcome)",
            w._stages(out1["op"]) == ["queued", "running", out1["status"]],
            w._stages(out1["op"]))

    # ── отказ safety ДО эффекта ─────────────────────────────────────────
    w2, _ = one()
    t2 = Recorder()
    out = w2.submit("olga", "restart", "pu-mop-1", {},
                    transport=t2,
                    safety=lambda name, project, **kw: (False, "led by anton"))
    c.check(what + "a refused safety never fires the effect",
            not t2.calls, t2.calls)
    c.check(what + "the refusal is the outcome",
            (out.get("status"), out.get("error")), ("refused", "led by anton"))

    # ── крах до RPC: intent есть, эффекта нет ───────────────────────────
    w3, _ = one()
    t3 = Recorder(fail_at={"*": "bus down"})
    out = w3.submit("anton", "restart", "pu-mop-1", {},
                    transport=t3,
                    safety=lambda name, project, **kw: (True, None))
    c.check(what + "a crash before the RPC leaves a durable failure",
            out.get("status") == "failed", out)
    again = w3.read("anton", out["op"])
    c.check(what + "the failure reads back", again.get("status") == "failed", again)

    # ── перезапуск: running-запись помечается unknown ───────────────────
    # HYPOTHESIS: служба упала посреди операции; при подъёме незавершённая
    # запись обязана честно стать unknown, а не «выполнено» и не «провалено».
    w4, root4 = one()
    stuck = w4._begin_for_test("anton", "restart", "pu-mop-1", {})
    w5 = opworker.Worker(root4, now=lambda: 2_000.0, transport=Recorder(), sleep=lambda s: None)
    got = w5.read("anton", stuck)
    c.check(what + "a restart marks a stuck running record unknown",
            got.get("status") == "unknown", got)
    c.check(what + "unknown forbids the automatic retry",
            not w5._retryable(got), got)

    # ── чтение: актуальная admin-роль, actor-supplied ID не открывает ───
    w6, _ = one()
    op6 = w6._begin_for_test("anton", "restart", "pu-mop-1", {})
    c.check(what + "the actor reads his operation",
            w6.read("anton", op6, role="admin").get("op") == op6)
    c.check(what + "a demoted reader is refused",
            w6.read("anton", op6, role="user") is None)
    c.check(what + "a stranger's ID does not open the record",
            w6.read("olga", op6, role="admin") is None)

    # ── очередь: переполненная очередь не принимает ─────────────────────
    w7, _ = one()
    t7 = Recorder()
    full = w7.submit("anton", "restart", "pu-mop-1", {},
                     transport=t7,
                     safety=lambda name, project, **kw: (True, None))
    c.check(what + "the queue admits up to its limit",
            full.get("status") != "queue-full", full)
    w8 = opworker.Worker(tempfile.mkdtemp(prefix="mop-test-476-"), now=lambda: 1.0, observe_wait=0, sleep=lambda s: None)
    w8.LIMIT = 1
    t8 = Recorder()
    # Одна незавершённая -- весь лимит: новая не принимается, и очередь
    # не растёт молча.
    w8._begin_for_test("anton", "restart", "pu-mop-1", {})
    a = w8.submit("olga", "restart", "pu-mop-2", {},
                  transport=t8, safety=lambda name, project, **kw: (True, None))
    c.check(what + "a full queue refuses new submissions",
            (a.get("status"), a.get("error")), ("queue-full", "the operations queue is full"))
    # Лимит -- атрибут воркера: подняли -- та же подача проходит.
    w8.LIMIT = 2
    b = w8.submit("olga", "restart", "pu-mop-2", {},
                  transport=t8, safety=lambda name, project, **kw: (True, None))
    c.check(what + "a raised limit admits the same submission",
            b.get("status") != "queue-full", b)

    # ── аудит: ops.record в каждую операцию, без message/workspace ─────
    # HYPOTHESIS: payload исполнителя (workspace, message) хранить в общем
    # журнале нельзя -- это чужие секреты; но и терять нельзя: воркеру они
    # нужны на эффект. SOLUTION: payload -- отдельный приватный файл со
    # своим сроком, из GET результатов его нет.
    w9, root9 = one()
    t9 = Recorder()
    w9.submit("anton", "restart", "pu-mop-1", {"note": "do it"},
              transport=t9, safety=lambda name, project, **kw: (True, None))
    listed = w9.read("anton", role="admin")
    c.check(what + "the result list carries no executor payload",
            not any(k in json.dumps(listed) for k in ("note", "workspace", "message")),
            listed)
    c.check(what + "every operation has an audit record",
            all(("principal" in r and "sender" in r) for r in w9._audit()), w9._audit())

    # ── сериализация конфликтующих действий по цели ─────────────────────
    # Пока по цели идёт незавершённая операция, следующая по той же цели
    # остаётся queued -- устойчивая идентичность цели, не поток: операция,
    # принятая до краха, держит очередь и после подъёма.
    w10, _ = one()
    t10 = Recorder()
    stuck = w10._begin_for_test("anton", "restart", "pu-mop-1", {})
    got = w10.submit("olga", "restart", "pu-mop-1", {"force": False},
                     transport=t10,
                     safety=lambda name, project, **kw: (True, None))
    c.check(what + "a second action on a busy target stays queued",
            (got.get("status"), t10.calls), ("queued", []))
    c.check(what + "another target is not blocked",
            w10.submit("olga", "restart", "pu-mop-2", {},
                       transport=t10,
                       safety=lambda name, project, **kw: (True, None))
            .get("status") in opworker.TERMINAL)
    # Цель освободилась -- ждущая доводится до исхода (drive).
    w10._mark(w10._ops[stuck], "succeeded")
    w10._drive("pu-mop-1", t10, lambda name, project, **kw: (True, None),
               None, None)
    c.check(what + "the waiting operation runs after the target frees",
            w10.read("olga", got["op"]).get("status") in opworker.TERMINAL,
            w10.read("olga", got["op"]))

    # ── crash-инъекции: до и после remote effect ───────────────────────
    # До эффекта: intent записан, статуса эффекта нет -- после подъёма
    # запись unknown (не failed: эффекта могли и не быть).
    wa, roota = one()
    wa._begin_for_test("anton", "restart", "pu-mop-1", {})
    wb = opworker.Worker(roota, now=lambda: 5_000.0, observe_wait=0, sleep=lambda s: None)
    c.check(what + "a crash before the effect leaves unknown, not failed",
            wb.read("anton", wb._ops and list(wb._ops)[0]).get("status") == "unknown",
            list(wb._ops.values()))
    # После эффекта, до записи исхода: исход неизвестен -- unknown, и
    # автоматического повтора нет.
    wc, rootc = one()
    tc = Recorder()
    ran = []
    got = wc.submit("anton", "restart", "pu-mop-1", {},
                    transport=tc, safety=lambda name, project, **kw: (True, None),
                    effect=lambda: (ran.append(1), {"outcome": "registered"})[1])
    c.check(what + "an effect that returned is recorded",
            (got.get("status"), bool(ran)), ("succeeded", True))
    # ...а вот эффект, за которым упали ДО записи (крах в _mark), виден
    # как unknown после подъёма: проверяем подменой _append на падение.
    wd, rootd = one()
    td = Recorder()
    with patched(wd, _append=lambda rec: (_ for _ in ()).throw(OSError("disk full"))):
        crashed = wd.submit("anton", "restart", "pu-mop-2", {},
                            transport=td,
                            safety=lambda name, project, **kw: (True, None))
    c.check(what + "a storage failure fails the submission loudly",
            crashed.get("status") == "failed" or crashed.get("error"), crashed)
    # Хранилище недоступно -- операция не запускается вовсе.
    we = opworker.Worker(tempfile.mkdtemp(prefix="mop-test-476-"), now=lambda: 1.0, observe_wait=0, sleep=lambda s: None)
    te = Recorder()
    import os as _os
    _os.chmod(we._root, 0o500)
    try:
        dead = we.submit("anton", "restart", "pu-mop-1", {},
                         transport=te,
                         safety=lambda name, project, **kw: (True, None))
        c.check(what + "a dead storage does not run the effect",
                not te.calls, te.calls)
    finally:
        _os.chmod(we._root, 0o700)
    # Хранилище живо снова -- приём работает (не залипло навсегда).
    ok_again = we.submit("anton", "restart", "pu-mop-3", {},
                         transport=te,
                         safety=lambda name, project, **kw: (True, None))
    c.check(what + "the storage recovers for new submissions",
            ok_again.get("op") is not None, ok_again)

    # ── retention и tombstone ───────────────────────────────────────────
    # После очистки старый ключ нельзя молча считать новым безопасным
    # запросом: дедуп не обещан навсегда, но и не молчит о прошлом.
    wf, rootf = one(now=None)
    old = wf.submit("anton", "restart", "pu-mop-1", {}, transport=Recorder(),
                    key="old-click",
                    safety=lambda name, project, **kw: (True, None))
    wf._purge(before=old["at"] + 1)
    c.check(what + "purged records leave no readable history",
            not wf.read("anton", role="admin"), wf.read("anton", role="admin"))
    tomb = wf._tombstones()
    c.check(what + "a purged key leaves a tombstone",
            any(t.get("key") == "old-click" for t in tomb), tomb)
    # Тот же ключ ПОСЛЕ очистки: tombstone не даёт молча выдать старый
    # ключ за безопасный новый -- ответ отказ, не «новая операция».
    replayed = wf.submit("anton", "restart", "pu-mop-1", {}, transport=Recorder(),
                         key="old-click",
                         safety=lambda name, project, **kw: (True, None))
    c.check(what + "a replayed purged key is refused, not silently new",
            replayed.get("status") == "refused", replayed)

    # ── наблюдаемый рестарт (#464): база в журнале ДО эффекта ───────────
    # HYPOTHESIS: воркер, зовя наблюдаемый рестарт, берёт снимок задачи тем
    # же read-глаголом; без записи базы ДО эффекта краш службы между RPC и
    # исходом оставлял бы донаблюдению не от чего отсчитывать сигнал.
    # SOLUTION: снимок -- в журнал (baseline) до RPC эффекта; recovery
    # (#476) донаблюдает read-only тем же сигналом (ops.observe_restart),
    # эффект не переигрывается никогда.
    class Ticking(Recorder):
        """alloc растит счётчик рестартов после первого эффекта."""
        def __init__(self):
            super().__init__()
            self.fired = False

        def __call__(self, verb, **fields):
            if verb == "restart":
                self.fired = True
            got = super().__call__(verb, **fields)
            if verb == "alloc" and self.fired:
                got = {"alloc": dict(self.ALLOC, task=dict(self.ALLOC["task"],
                                                           restarts=8))}
            return got
    wg, rootg = one()
    tg = Ticking()
    got = wg.submit("anton", "restart", "pu-mop-1", {},
                    transport=tg, safety=lambda name, project, **kw: (True, None))
    c.check(what + "an observed restart whose signal arrives succeeds",
            got.get("status") == "succeeded" and got.get("restarts") == 8, got)
    c.check(what + "the journal carries the baseline",
            wg.read("anton", got["op"]).get("baseline", {}).get("task", {})
            .get("restarts") == 7, wg.read("anton", got["op"]))
    # Сигнала нет (счётчик стоит) -- unknown, не succeeded: прежний running
    # не успех (#464).
    wh, _ = one()
    th = Recorder()
    got = wh.submit("anton", "restart", "pu-mop-1", {},
                    transport=th, safety=lambda name, project, **kw: (True, None))
    c.check(what + "a standing old running is unknown, not succeeded",
            got.get("status") == "unknown"
            and "no restart seen" in (got.get("error") or ""), got)
    # Задача вернулась падшей -- partial с причиной.
    class Falling(Recorder):
        def __call__(self, verb, **fields):
            got = super().__call__(verb, **fields)
            if verb == "alloc" and self.fired:
                return {"alloc": dict(self.ALLOC,
                                      task=dict(self.ALLOC["task"], restarts=8,
                                                failed=True, state="dead"))}
            if verb == "restart":
                self.fired = True
            return got
    wi, _ = one()
    ti = Falling()
    got = wi.submit("anton", "restart", "pu-mop-1", {},
                    transport=ti, safety=lambda name, project, **kw: (True, None))
    c.check(what + "a task dead after the restart is partial",
            got.get("status") == "partial", got)

    # ── recovery-observation (#464 x #476) ──────────────────────────────
    # Журнальная база есть, исход не записан: подъём службы донаблюдает
    # read-only тем же сигналом. Эффект не переигрывается: рестарт-RPC в
    # transport восстановления не приходит.
    class RecoveryTransport(Recorder):
        """transport нового процесса: сигнал рестарта уже в кластере."""
        def __init__(self):
            super().__init__()
            self.restarts = 0
        def __call__(self, verb, **fields):
            if verb == "restart":
                self.restarts = 99      # маркер переигрывания
            got = super().__call__(verb, **fields)
            if verb == "alloc":
                got = {"alloc": dict(self.ALLOC,
                                     task=dict(self.ALLOC["task"],
                                               restarts=8 + self.restarts))}
            return got
    # Запись, остановленная ПОСЛЕ RPC с базой в журнале: running + baseline.
    wj, rootj = one()
    tj = RecoveryTransport()
    stuck = wj._begin_for_test("anton", "restart", "pu-mop-1", {})
    wj._mark(wj._ops[stuck], "running", baseline={"ID": "a1", "task": {"restarts": 7}})
    wk = opworker.Worker(rootj, now=lambda: 2_000.0, transport=tj,
                         observe_wait=0, sleep=lambda s: None)
    got = wk.read("anton", stuck)
    c.check(what + "recovery observes the journal's baseline to a verdict",
            got.get("status") == "succeeded" and got.get("restarts") == 8, got)
    c.check(what + "recovery never replays the effect",
            tj.restarts == 0, tj.calls)
    # Транспорта у нового процесса нет -- unknown с причиной, не падение.
    wl, rootl = one()
    stuck2 = wl._begin_for_test("anton", "restart", "pu-mop-1", {})
    wl._mark(wl._ops[stuck2], "running", baseline={"ID": "a1", "task": {"restarts": 7}})
    wm = opworker.Worker(rootl, now=lambda: 3_000.0, observe_wait=0,
                         sleep=lambda s: None)
    got = wm.read("anton", stuck2)
    c.check(what + "recovery without a transport stays unknown",
            got.get("status") == "unknown", got)
    # Queued-запись (до RPC, базы нет) -- прежняя судьба: unknown.
    wn, rootn = one()
    stuck3 = wn._begin_for_test("anton", "restart", "pu-mop-1", {})
    wo = opworker.Worker(rootn, now=lambda: 4_000.0, transport=Recorder(),
                         observe_wait=0, sleep=lambda s: None)
    c.check(what + "a queued record without baseline stays unknown",
            wo.read("anton", stuck3).get("status") == "unknown",
            wo.read("anton", stuck3))

    return c.report("opworker")


if __name__ == "__main__":
    sys.exit(main())
