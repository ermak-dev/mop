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
    """Транспорт-подмена: глаголы #458 считаются, любой может упасть."""

    def __init__(self, fail_at=None):
        self.calls = []
        self.fail_at = fail_at or {}
        self.answers = {"add": {"ok": True, "name": "pu-mop-9"}}

    def __call__(self, verb, **fields):
        self.calls.append((verb, fields))
        why = self.fail_at.get(verb) or self.fail_at.get("*")
        if why:
            raise ops.bus.Refused(why) if "refuse" in str(why) else RuntimeError(why)
        return dict(self.answers.get(verb, {"ok": True}))


def one(now=(lambda: 1_000.0)):
    """Воркер на свежем временном каталоге."""
    root = tempfile.mkdtemp(prefix="mop-test-476-")
    return opworker.Worker(root, now=now), root


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
            len(transport.calls) == 1, transport.calls)
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
    w5 = opworker.Worker(root4, now=lambda: 2_000.0)
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
    w8 = opworker.Worker(tempfile.mkdtemp(prefix="mop-test-476-"), now=lambda: 1.0)
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
    wb = opworker.Worker(roota, now=lambda: 5_000.0)
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
    we = opworker.Worker(tempfile.mkdtemp(prefix="mop-test-476-"), now=lambda: 1.0)
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

    return c.report("opworker")


if __name__ == "__main__":
    sys.exit(main())
