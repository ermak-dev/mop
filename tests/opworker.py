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
import time

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import bus, ops, puppets, state  # noqa: E402
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

    # ── add/update поверх общих операций (#470) ─────────────────────────
    # HYPOTHESIS: воркер умеет только restart; адаптер (#461) ставит add в
    # очередь, а исполнителя у глагола нет -- «no library operation». У веба
    # нет рабочей копии: workspace обязан приезжать из ЯВНОГО удалённого
    # источника (fetch origin -- как его читает manifest), никаких cwd-чтений
    # и локальных файлов сервера; origin add обязан быть в реестре -- иначе
    # отказ до эффекта. fresh у update -- явный bool, молчаливого режима нет.
    # SOLUTION: _library_effect зовёт ops.add/ops.update (#458); источник
    # workspace -- remote_workspace(origin), injectируется проверками;
    # реестр -- callable; цель add -- сам origin (дедуп и сериализация по
    # нему), присвоенное имя -- факт записи.
    PROV = {"source": "origin", "commit": "c1", "dirty": False,
            "tasks": 1, "present": True}

    class AddT(Recorder):
        """Ответы сервиса для add: имя, метка, подъём, свой итог прогона."""

        def __init__(self):
            super().__init__()
            self.answers = {"add": {"ok": True, "name": "pu-mop-9",
                                    "bootstrap_marker": "m1"},
                            "alloc": {"alloc": dict(self.ALLOC)},
                            "bootstrap_result": {"result": {"sent": "m1",
                                                            "played": True,
                                                            "ok": True}}}

    def web_worker(registry=("git@h:g/x.git",), workspace=None):
        root = tempfile.mkdtemp(prefix="mop-test-470-")
        # Выкачки дефолтного источника считаются: w._fetched -- список origin.
        seen = []
        core = workspace or (lambda o: (f"boot for {o}", dict(PROV)))
        w = opworker.Worker(root, now=lambda: 1_000.0, observe_wait=0,
                            sleep=lambda s: None,
                            registry=(lambda: set(registry))
                            if registry is not None else None,
                            workspace=lambda o: (seen.append(o), core(o))[1])
        w._fetched = seen
        return w, root

    # Успешный add: workspace уходит из явного источника, одна выкачка.
    fetched = []
    w, _ = web_worker(workspace=lambda o: (fetched.append(o),
                                           f"boot for {o}", dict(PROV))[1:])
    t = AddT()
    got = w.submit("anton", "add", None,
                   {"origin": "git@h:g/x.git", "branch": "dev"},
                   transport=t, safety=lambda name, project, **kw: (True, None))
    c.check("#470 add through the worker succeeds with #476 verdicts",
            got.get("status") == "succeeded", got)
    c.check("#470 add rides the explicit remote workspace",
            t.calls and t.calls[0] == ("add", {"origin": "git@h:g/x.git",
                                               "timeout": 30,
                                               "workspace": "boot for git@h:g/x.git",
                                               "bootstrap_sent": PROV,
                                               "branch": "dev"}), t.calls[:1])
    c.check("#470 the workspace is fetched once, from the given origin",
            fetched == ["git@h:g/x.git"], fetched)
    c.check("#470 the assigned puppet name lands in the record",
            w.read("anton", got["op"]).get("name") == "pu-mop-9",
            w.read("anton", got["op"]))

    # Незарегистрированный origin -- отказ ДО эффекта (кнопки нет -- и
    # обходной дороги нет: воркер сам gates).
    w, _ = web_worker(registry=("git@h:g/other.git",))
    t = AddT()
    got = w.submit("anton", "add", None, {"origin": "git@h:g/x.git"},
                   transport=t, safety=lambda name, project, **kw: (True, None))
    c.check("#470 an unregistered origin is refused before any effect",
            got.get("status") == "refused" and "registry" in (got.get("error") or "")
            and not t.calls, (got, t.calls))
    # Реестр не подключён -- отказ (fail closed), не тихий пропуск.
    w, _ = web_worker(registry=None)
    got = w.submit("anton", "add", None, {"origin": "git@h:g/x.git"},
                   transport=AddT(),
                   safety=lambda name, project, **kw: (True, None))
    c.check("#470 add without a wired registry is refused, not waved through",
            got.get("status") == "refused", got)

    # Цель add -- сам origin: дедуп по нему, сериализация по нему; имени
    # папета ещё нет, его присвоит сервис.
    w, _ = web_worker()
    a1 = w.submit("anton", "add", None, {"origin": "git@h:g/x.git"},
                  transport=AddT(),
                  safety=lambda name, project, **kw: (True, None))
    a2 = w.submit("anton", "add", None, {"origin": "git@h:g/x.git"},
                  transport=AddT(),
                  safety=lambda name, project, **kw: (True, None))
    c.check("#470 a repeated add of one origin is the same operation",
            a1.get("op") == a2.get("op"), (a1, a2))
    stuck = w._begin_for_test("anton", "add", "git@h:g/x.git",
                              {"origin": "git@h:g/x.git"})
    t = AddT()
    # Другой actor -- другой ключ дедупа: та же операция не вернётся,
    # новая встанет за незавершённой (сериализация по цели=origin).
    waiting = w.submit("olga", "add", None, {"origin": "git@h:g/x.git"},
                       transport=t,
                       safety=lambda name, project, **kw: (True, None))
    c.check("#470 a second add of one origin waits, fires nothing",
            waiting.get("status") == "queued" and not t.calls, (waiting, t.calls))
    other = w.submit("anton", "add", None, {"origin": "git@h:g/y.git"},
                     transport=AddT(),
                     safety=lambda name, project, **kw: (True, None))
    c.check("#470 an add of another origin does not wait",
            other.get("status") in opworker.TERMINAL, other)

    # Payload не входит в аудит: текст bootstrap'а живёт только в вызове
    # операции, в журнале, в GET и в payload-файле его нет.
    w, root = web_worker(workspace=lambda o: ("BOOT-SECRET-470", dict(PROV)))
    got = w.submit("anton", "add", None, {"origin": "git@h:g/x.git"},
                   transport=AddT(),
                   safety=lambda name, project, **kw: (True, None))
    with open(w._log, encoding="utf-8") as f:
        journal = f.read()
    c.check("#470 the bootstrap text never enters the journal",
            "BOOT-SECRET-470" not in journal, journal[-200:])
    c.check("#470 the bootstrap text never enters GET",
            "BOOT-SECRET-470" not in json.dumps(w.read("anton", role="admin")),
            w.read("anton", role="admin"))
    with open(os.path.join(root, "payload", f"{got['op']}.json"),
              encoding="utf-8") as f:
        payload = json.load(f)
    c.check("#470 the payload file holds the request, not the fetched text",
            "BOOT-SECRET-470" not in json.dumps(payload)
            and payload.get("request") == {"origin": "git@h:g/x.git"}, payload)
    c.check("#470 the audit record carries no workspace",
            "BOOT-SECRET-470" not in json.dumps(w._audit()), w._audit())

    # ── update через воркер: fresh явный, источник -- origin папета ─────
    # keep (fresh false): origin прежний (из спеки), workspace -- из него
    # же; спека читается ОДИН раз и едет в операцию (не спрашивать вторично).
    w, _ = web_worker()
    t = Recorder()
    t.answers = {"spec": {"meta": {"origin": "git@h:g/x.git"}},
                 "update": {"replaced": None}}
    got = w.submit("anton", "update", "pu-mop-1", {"fresh": False},
                   transport=t, safety=lambda name, project, **kw: (True, None))
    c.check("#470 a keep update reads as partial (accepted, no run)",
            got.get("status") == "partial", got)
    c.check("#470 a keep update keeps the origin and resumes",
            len(t.calls) > 1 and t.calls[1] == ("update",
                {"name": "pu-mop-1", "origin": "git@h:g/x.git", "cont": True,
                 "new_origin": None, "workspace": "boot for git@h:g/x.git",
                 "bootstrap_sent": PROV, "owner": "anton", "force": False,
                 "branch": None}), t.calls[1:])
    c.check("#470 a keep update fetches the puppet's own origin",
            w._fetched == ["git@h:g/x.git"], w._fetched)
    c.check("#470 the spec is read once and rides into the operation",
            [verb for verb, _ in t.calls].count("spec") == 1, t.calls)
    # remove (fresh true + origin): workspace -- из НОВОГО origin.
    w, _ = web_worker()
    t = Recorder()
    t.answers = {"spec": {"meta": {"origin": "git@h:g/x.git"}},
                 "update": {"bootstrap_marker": "m3"},
                 "bootstrap_result": {"result": {"sent": "m3", "played": True,
                                                 "ok": True}}}
    got = w.submit("anton", "update", "pu-mop-1",
                   {"fresh": True, "origin": "git@h:g/new.git"},
                   transport=t, safety=lambda name, project, **kw: (True, None))
    c.check("#470 a remove update succeeds through the worker",
            got.get("status") == "succeeded", got)
    fields = t.calls[1][1]
    c.check("#470 a remove update rides the NEW origin's workspace",
            fields.get("cont") is False
            and fields.get("new_origin") == "git@h:g/new.git"
            and fields.get("workspace") == "boot for git@h:g/new.git", fields)
    c.check("#470 a remove update fetches only the new origin",
            w._fetched == ["git@h:g/new.git"], w._fetched)

    # ── safety-матрица на update (#460/#470): отказ ДО эффекта ──────────
    NOW = 1_000_000.0
    CLEAN = {"cur": "master", "def": "master", "dirty": 0, "ahead": 0}

    def bound_safety(facts):
        return lambda name, project, owner: ops.safety(
            name, project, owner=owner, facts=facts, now=NOW)

    def update_worker_with(facts):
        w, _ = web_worker()
        t = Recorder()
        t.answers = {"spec": {"meta": {"origin": "git@h:g/x.git"}},
                     "update": {"replaced": None}}
        return w, t

    for what, facts in (
            ("a dirty clone", {"clone": dict(CLEAN, dirty=2, owner=None)}),
            ("a foreign live lease",
             {"clone": dict(CLEAN, owner={"user": "olga", "at": NOW - 5})}),
            ("unknown facts", None),
            ("an agent error", {"error": "silent"})):
        w, t = update_worker_with(facts)
        got = w.submit("anton", "update", "pu-mop-1", {"fresh": False},
                       transport=t, safety=bound_safety(facts))
        c.check(f"#470 update refuses {what} before the effect",
                got.get("status") == "refused" and not t.calls, (got, t.calls))
    ok, why = bound_safety({"clone": dict(CLEAN, owner={"user": "olga",
                                                        "at": NOW - 5})})(
        "pu-mop-1", None, "anton")
    c.check("#470 the foreign-lease refusal names the holder",
            not ok and "olga" in (why or ""), why)
    # Своя живая аренда на чистом клоне -- проход, эффект исполняется.
    w, t = update_worker_with(None)
    got = w.submit("anton", "update", "pu-mop-1", {"fresh": False},
                   transport=t,
                   safety=bound_safety({"clone": dict(CLEAN, owner={"user": "anton",
                                                                    "at": NOW - 5})}))
    c.check("#470 update with the owner's own clean lease runs",
            got.get("status") == "partial" and t.calls, (got, t.calls))

    # Выкачка не удалась -- failed громко, с фразой без чужого stderr.
    def broken_source(origin):
        raise RuntimeError("cannot read x: access denied")
    w, _ = web_worker(workspace=broken_source)
    got = w.submit("anton", "add", None, {"origin": "git@h:g/x.git"},
                   transport=AddT(),
                   safety=lambda name, project, **kw: (True, None))
    c.check("#470 a failed workspace fetch fails loudly",
            got.get("status") == "failed" and "access denied" in got.get("error", ""),
            got)

    # ── remote_workspace: происхождение тем же видом, что у CLI (#334) ──
    # Только origin-источник: fetch_tree и cwd дороги не касаются вовсе.
    rs = getattr(opworker, "remote_workspace", None)
    if c.check("#470 remote_workspace exists", rs is not None):
        from mop.common import manifest
        WS_TEXT = ("- name: ws\n  vars:\n    A: 1\n  tasks:\n"
                   "    - name: t1\n      ansible.builtin.debug: {msg: hi}\n"
                   "    - name: t2\n      ansible.builtin.debug: {msg: ho}\n")

        def boom(*a, **k):
            raise AssertionError("fetch_tree must not be touched by the web")
        with patched(manifest, fetch_tree=boom):
            text, prov = rs("git@h:g/x.git",
                            fetch=lambda o: {"bootstrap_text": WS_TEXT,
                                             "commit": "abc123"})
        c.expect("#470 the remote provenance carries the CLI shape",
                 prov, {"source": "origin", "commit": "abc123", "dirty": False,
                        "tasks": 2, "present": True})
        c.check("#470 the remote text is the file's text", text == WS_TEXT, text)
        text, prov = rs("o", fetch=lambda o: {"bootstrap_text": "",
                                              "commit": "abc"})
        c.expect("#470 no file means present false and zero tasks",
                 prov, {"source": "origin", "commit": "abc", "dirty": False,
                        "tasks": 0, "present": False})
        text, prov = rs("o", fetch=lambda o: {"bootstrap_text": "vars: {}\n",
                                              "commit": None})
        c.check("#470 a malformed file reads as tasks unknown",
                prov.get("tasks") is None and prov.get("present") is True, prov)

    # ── жизненный цикл через воркер (#471): recycle/delete/wipe ─────────
    # HYPOTHESIS: у веб-клика те же общие реализации puppets.recycle/delete/
    # wipe (#458), что и у CLI; вторая реализация разошлась бы с первой в
    # тот же день. До регистрации воркер честно отвечает, что операции нет.
    # SOLUTION: глаголы регистрируются в _library_effect обёрткой со
    # структурным результатом (outcome-словарь #458) поверх готовых функций;
    # safety -- disruptive перед эффектом, аудит и адресация субъектом
    # проекта цели -- как у restart.
    # RESULT: 12 RED-checks (no library operation) -> wrappers in
    # mop/server/opworker.py; матрица и сериализация -- тем же ходом.
    # STATUS: FIXED — see #471
    t471 = Recorder()
    w471, _ = one()
    got = w471.submit("anton", "reboot", "pu-mop-1", {}, transport=t471,
                      safety=lambda name, project, **kw: (True, None))
    c.check("#471 a verb with no library operation is refused, not crashed",
            (got.get("status"), got.get("error"))
            == ("failed", "no library operation for reboot"), got)

    # Подмена общего слоя: эффекты считаются вызовами, ответы -- структурой
    # реальных функций (tests/recycle.py, tests/delete.py).
    class Lifecycle:
        """Подмена puppets.recycle/delete/wipe: вызовы и их ответы."""

        def __init__(self, result=None, error=None):
            self.calls = []
            self.result = result or {}
            self.error = error

        def recycle(self, name, **kw):
            self.calls.append(("recycle", name, kw))
            if self.error:
                raise RuntimeError(self.error)
            return dict(self.result, node="n1")

        def delete(self, name, **kw):
            self.calls.append(("delete", name, kw))
            if self.error:
                raise RuntimeError(self.error)
            return dict(self.result, node="n1", body="destroyed")

        def wipe(self, node, name, **kw):
            self.calls.append(("wipe", node, name, kw))
            if self.error:
                raise RuntimeError(self.error)
            return {"reset": True, "target": "target"}

        def node_of(self, name, running=True):
            self.calls.append(("node_of", name, {"running": running}))
            return "n9"


    def lifecycle_worker(lc):
        w, _ = one()
        return w

    def run_verb(verb, lc, facts=None, busy=False, safety=None):
        """Воркер, чей library-эффект зовёт общий слой подменой."""
        w, _ = one()
        asked = []
        with patched(puppets, recycle=lc.recycle, delete=lc.delete,
                     wipe=lc.wipe, node_of=lc.node_of):
            got = w.submit("anton", verb, "pu-mop-1", {}, transport=t471,
                           safety=safety or (lambda name, project, **kw: (True, None)))
        return got

    # Успешный путь каждого глагола через воркер: эффект -- общий, вердикт --
    # карта исходов #476. registered -> succeeded.
    lc = Lifecycle()
    got = run_verb("recycle", lc)
    c.check("#471 recycle through the worker succeeds",
            got.get("status") == "succeeded", got)
    c.check("#471 recycle calls the common implementation once",
            [(c[0], c[1]) for c in lc.calls] == [("recycle", "pu-mop-1")], lc.calls)
    c.check("#471 recycle carries no force from the web",
            lc.calls and lc.calls[0][2].get("force") is False, lc.calls)

    lc = Lifecycle()
    got = run_verb("delete", lc)
    c.check("#471 delete through the worker succeeds",
            got.get("status") == "succeeded", got)
    c.check("#471 delete calls the common implementation",
            [(c[0], c[1]) for c in lc.calls] == [("delete", "pu-mop-1")], lc.calls)

    lc = Lifecycle()
    got = run_verb("wipe", lc)
    c.check("#471 wipe through the worker succeeds",
            got.get("status") == "succeeded", got)
    c.check("#471 wipe resolves the node stopped and wipes there",
            [(c[0], c[1]) for c in lc.calls] == [("node_of", "pu-mop-1"),
                                                 ("wipe", "n9")]
            and lc.calls[1][2] == "pu-mop-1"
            and lc.calls[1][3].get("force") is False,
            lc.calls)

    # Partial (#476): recycle поднял папета, наблюдение не сошлось --
    # replaced=False общей операции (спека не изменилась, клон не поднимется)
    # читается как partial, а не тишина.
    class AcceptedLifecycle(Lifecycle):
        def recycle(self, name, **kw):
            super().recycle(name, **kw)
            return {"replaced": False, "node": "n1"}

    lc = AcceptedLifecycle()
    got = run_verb("recycle", lc)
    c.check("#471 recycle without its confirmation reads partial",
            got.get("status") == "partial", got)

    # Отказ эффекта -- failed с причиной, не тишина.
    lc = Lifecycle(error="pu-mop-1 has no allocation — nothing to recycle")
    got = run_verb("recycle", lc)
    c.check("#471 a failing effect is failed with the reason",
            (got.get("status"), "no allocation" in (got.get("error") or ""))
            == ("failed", True), got)

    # Живое отказ агента у wipe (#40): tmux-сессия жива -- refused, как
    # в CLI (puppets.wipe отвечает RuntimeError'ом с полем error агента).
    lc = Lifecycle(error="pu-mop-1: tmux session is alive — stop the job first")
    got = run_verb("wipe", lc)
    c.check("#471 wipe on a live session is refused, as in the CLI",
            (got.get("status"), "alive" in (got.get("error") or ""))
            == ("refused", True), got)

    # ── safety-матрица (#460) на каждом из трёх, disruptive ─────────────
    # Dirty-клон, чужая живая аренда, busy-сессия и неизвестные факты --
    # отказ ДО эффекта на каждом глаголе; эффект не зовётся вовсе. Предикат
    # здесь -- сам ops.safety, но disruptive воркеру задаёт РЕГИСТРАЦИЯ:
    # какой kw она передала, проверяет ProbingSafety ниже.
    class ProbingSafety:
        """ops.safety как её зовёт воркер: kw пишутся, disruptive читается."""

        def __init__(self, facts, busy):
            self.facts, self.busy = facts, busy
            self.seen = {}

        def __call__(self, name, project, **kw):
            self.seen = kw
            return ops.safety(name, project, owner=kw.get("owner"),
                              facts=self.facts, busy=self.busy,
                              disruptive=self.disruptive(kw), now=NOW)

        @staticmethod
        def disruptive(kw):
            return bool(kw.get("disruptive"))

    NOW = 1_000_000.0
    CL = {"cur": "master", "def": "master", "dirty": 0, "ahead": 0}
    DIRTY_CL = {"cur": "epic/x", "def": "master", "dirty": 3, "ahead": 1}
    MATRIX = (
        ("a dirty clone", {"clone": dict(DIRTY_CL, owner=None)}),
        ("a foreign live lease",
         {"clone": dict(CL, owner={"user": "olga", "at": NOW - 5})}),
        ("a busy session", {"clone": dict(CL, owner={"user": "anton", "at": NOW - 5})}),
        ("unknown facts", None),
        ("an agent error", {"error": "agent silent"}),
    )
    for verb in ("recycle", "delete", "wipe"):
        for what, facts in MATRIX:
            lc = Lifecycle()
            busy = what == "a busy session"
            probe = ProbingSafety(facts, busy)
            got = run_verb(verb, lc, safety=probe)
            refused = got.get("status") == "refused"
            c.check(f"#471 {verb} refuses on {what}",
                    refused and bool(got.get("error")), got)
            c.check(f"#471 {verb} on {what} never fires the effect",
                    not lc.calls, lc.calls)

        # Регистрация обязана пометить глагол разрушительным: busy-сессия
        # (своя аренда, чистый клон) доходит до safety и отпирается им.
        lc = Lifecycle()
        probe = ProbingSafety({"clone": dict(CL, owner={"user": "anton",
                                                       "at": NOW - 5})}, True)
        got = run_verb(verb, lc, safety=probe)
        c.check(f"#471 {verb} is registered disruptive",
                probe.seen.get("disruptive") is True, probe.seen)

    # Своя аренда на чистом клоне, сессия не занята -- проход (не матрица
    # отказа): воркер зовёт safety disruptive=True сам.
    lc = Lifecycle()
    ok_safety = lambda name, project, **kw: ops.safety(
        name, project, owner=kw.get("owner"),
        facts={"clone": dict(CL, owner={"user": "anton", "at": NOW - 5})},
        busy=False, disruptive=True, now=NOW)
    got = run_verb("recycle", lc, safety=ok_safety)
    c.check("#471 own lease on a clean idle clone passes to the effect",
            got.get("status") == "succeeded" and len(lc.calls) == 1, got)

    # ── аудит и payload (#476: без message/workspace) ────────────────────
    lc = Lifecycle()
    got = run_verb("recycle", lc)
    listed = w471.read("anton", role="admin")
    # Чтение -- другой воркер, здесь важен ответ операции: без payload.
    c.check("#471 the operation answer carries no executor payload",
            not any(k in json.dumps(got) for k in ("workspace", "message",
                                                   "request", "params")), got)
    w471b, root471 = one()
    with patched(puppets, recycle=lc.recycle, delete=lc.delete,
                 wipe=lc.wipe, node_of=lc.node_of):
        rec = w471b.submit("anton", "recycle", "pu-mop-1", {},
                           transport=t471,
                           safety=lambda name, project, **kw: (True, None))
    listed = w471b.read("anton", role="admin")
    c.check("#471 the record list carries no lifecycle payload",
            not any(k in json.dumps(listed)
                    for k in ("workspace", "message", "request")), listed)
    c.check("#471 every lifecycle operation has an audit record",
            all(("principal" in r and "sender" in r) for r in w471b._audit()),
            w471b._audit())
    # Payload-файл -- приватный, но не в журнале: журнал без workspace.
    with open(os.path.join(root471, "ops.jsonl")) as f:
        journal = f.read()
    c.check("#471 the journal carries no workspace text",
            "workspace" not in journal, journal[:400])

    # ── сериализация: recycle и delete одной цели не едут разом ─────────
    w471c, _ = one()
    stuck = w471c._begin_for_test("anton", "recycle", "pu-mop-1", {})
    lc = Lifecycle()
    with patched(puppets, recycle=lc.recycle, delete=lc.delete,
                 wipe=lc.wipe, node_of=lc.node_of):
        queued = w471c.submit("anton", "delete", "pu-mop-1", {},
                              transport=t471,
                              safety=lambda name, project, **kw: (True, None))
    c.check("#471 a delete queued behind a recycle of the same target waits",
            (queued.get("status"), lc.calls), ("queued", []))
    w471c._mark(w471c._ops[stuck], "succeeded")
    with patched(puppets, recycle=lc.recycle, delete=lc.delete,
                 wipe=lc.wipe, node_of=lc.node_of):
        w471c._drive("pu-mop-1", t471, lambda name, project, **kw: (True, None),
                     None, None)
    done = w471c.read("anton", queued["op"])
    c.check("#471 the queued delete runs after the target frees",
            done.get("status") == "succeeded" and len(lc.calls) == 1,
            (done, lc.calls))

    return c.report("opworker")


if __name__ == "__main__":
    sys.exit(main())
