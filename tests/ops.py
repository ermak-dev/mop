#!/usr/bin/env python3
"""Общие операции пула без шины: python3 tests/ops.py

HYPOTHESIS (#458): workflow add/update/restart жил в командлетах и только
там; вебу (#456) нужна та же операция, и копия разошлась бы с первой.
SOLUTION: mop/common/ops.py -- операции с transport-аргументом (проверки
подставляют свой), вердикт-словарь outcome() с видами
registered/accepted/refused/partial/unknown, общий прогон bootstrap'а по
метке и send_to_puppet из client/channel.py (тонкая обёртка осталась).
Командлеты пока зовут своё (#459 сносит их алгоритмы); здесь -- parity
поведений: тот же порядок эффектов, те же решения на тех же ответах.
STATUS: FIXED — see #458
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

try:
    from mop.common import bus, ops
except ImportError:
    ops = None


class Fake:
    """Подставной транспорт: ответы по (verb) очередями и записи вызовов."""

    def __init__(self, answers):
        self.answers = answers    # verb -> [ответы по очереди] | ответ
        self.calls = []

    def __call__(self, verb, **fields):
        self.calls.append((verb, fields))
        got = self.answers.get(verb)
        if isinstance(got, list):
            return got.pop(0) if got else {}
        return got if got is not None else {}

    def node(self, node, verb, **fields):
        # send_to_puppet зовёт транспорт узла: (node, verb, ...)
        self.calls.append((node, verb, fields))
        got = self.answers.get(verb)
        return got if got is not None else {}


def nosleep(_):
    pass


def stuck_clock():
    return 1_000_000.0


def stepping(stepped):
    return lambda s: stepped.append(s)


def main():
    c = Checks()
    if not c.check("mop.common.ops exists", ops is not None):
        return c.report("ops")

    # ── вердикт-словарь ─────────────────────────────────────────────────
    v = ops.outcome(ops.REGISTERED, "pu-x-1", ["a", "b"], error=None, marker="m")
    c.expect("outcome() shape",
             v, {"outcome": "registered", "name": "pu-x-1", "progress": ["a", "b"],
                 "error": None, "marker": "m"})
    c.check("outcome kinds are the five",
            (ops.REGISTERED, ops.ACCEPTED, ops.REFUSED, ops.PARTIAL, ops.UNKNOWN)
            == ("registered", "accepted", "refused", "partial", "unknown"))

    # ── add: порядок эффектов и итоги ────────────────────────────────────
    # Успех: add -> alloc(running) -> bootstrap_result(свою метку) -> registered.
    marker = {"sent": "m1"}
    fake = Fake({"add": {"name": "pu-x-1", "bootstrap_marker": "m1"},
                 "alloc": {"alloc": {"ClientStatus": "running",
                                     "TaskStates": {"claude": {"State": "running"}},
                                     "NodeID": "n1"}},
                 "bootstrap_result": {"result": dict(marker, played=True, ok=True)}})
    stepped = []
    v = ops.add(fake, "git@h:g/x.git", ("text", {"source": "origin"}),
                branch="dev", sleep=nosleep, clock=stuck_clock, step=stepping(stepped))
    verbs = [verb for verb, _ in fake.calls]
    c.check("add effect order: add, alloc, bootstrap_result",
            verbs == ["add", "alloc", "bootstrap_result"], verbs)
    c.expect("add registers the workspace and branch",
             fake.calls[0][1], {"origin": "git@h:g/x.git", "timeout": 30,
                                "workspace": "text",
                                "bootstrap_sent": {"source": "origin"},
                                "branch": "dev"})
    c.check("successful bootstrap -> registered",
            v["outcome"] == "registered" and v["marker"] == "m1", v)
    # Метка своей регистрации лежит первой же -- шагов ожидания нет;
    # шаги появляются только когда первый ответ чужой (см. ниже).
    c.check("immediate own marker needs no waiting steps", stepped == [], stepped)
    # Прогресс не содержит терминальных строк и секретов: то, что печатал
    # командлет после (-- итог), здесь нет вовсе.
    joined = " ".join(v["progress"]).lower()
    c.check("no terminal bootstrap lines in progress",
            "bootstrap failed" not in joined and "rc " not in joined, v["progress"])

    # Ошибка ПОСЛЕ регистрации: bootstrap лёг -- refused с задачей, не unknown.
    fake = Fake({"add": {"name": "pu-x-1", "bootstrap_marker": "m2"},
                 "alloc": {"alloc": {"ClientStatus": "running",
                                     "TaskStates": {"claude": {"State": "running"}}}},
                 "bootstrap_result": {"result": {"sent": "m2", "played": True,
                                                 "ok": False, "task": "install",
                                                 "message": "no space"}}})
    v = ops.add(fake, "git@h:g/x.git", ("", None), sleep=nosleep, clock=stuck_clock)
    c.check("failed bootstrap -> refused with the task",
            v["outcome"] == "refused" and "install" in (v.get("error") or ""), v)

    # ЧУЖОЙ маркер не читается как свой: ждём, потом таймаут -> partial.
    fake = Fake({"add": {"name": "pu-x-1", "bootstrap_marker": "mine"},
                 "alloc": {"alloc": {"ClientStatus": "running",
                                     "TaskStates": {"claude": {"State": "running"}}}},
                 "bootstrap_result": {"result": {"sent": "foreign", "played": True,
                                                 "ok": False}}})
    v = ops.add(fake, "git@h:g/x.git", ("", None), sleep=nosleep, clock=stuck_clock,
                bootstrap_wait=0)
    c.check("a foreign marker is not ours: partial on timeout",
            v["outcome"] == "partial" and v.get("bootstrap") is None, v)

    # Аллокация падает на старте -> unknown (не refused: отказа не было).
    fake = Fake({"add": {"name": "pu-x-1"},
                 "alloc": {"alloc": {"ClientStatus": "failed",
                                     "TaskStates": {"claude": {"State": "dead",
                                                               "Failed": True}}}}})
    v = ops.add(fake, "git@h:g/x.git", ("", None), sleep=nosleep, clock=stuck_clock)
    c.check("failed allocation -> unknown", v["outcome"] == "unknown", v)

    # Аллокация не встала за срок -> unknown, место называется.
    fake = Fake({"add": {"name": "pu-x-1"}, "alloc": {}})
    v = ops.add(fake, "git@h:g/x.git", ("", None), sleep=nosleep, clock=stuck_clock,
                place_wait=0)
    c.check("no placement in time -> unknown", v["outcome"] == "unknown", v)

    # Отказ шины при регистрации -> refused.
    def refusing(verb, **fields):
        raise bus.Refused("held by someone")
    v = ops.add(refusing, "git@h:g/x.git", ("", None), sleep=nosleep, clock=stuck_clock)
    c.check("refused registration -> refused", v["outcome"] == "refused", v)

    # ── update: no-op, fresh, смена origin ──────────────────────────────
    meta = {"origin": "git@h:g/old.git"}
    # No-op: спека не изменилась, метки нет -- accepted, не registered.
    fake = Fake({"spec": {"meta": meta},
                 "update": {"replaced": None}})
    v = ops.update(fake, "pu-x-1", sleep=nosleep, clock=stuck_clock)
    c.check("no-op update -> accepted",
            v["outcome"] == "accepted" and "bootstrap_marker" not in v, v)
    c.check("no-op keeps origin and resumes (cont=True)",
            fake.calls[1][1].get("cont") is True
            and fake.calls[1][1].get("new_origin") is None, fake.calls[1][1])

    # Смена origin без --fresh: история каталога не переживает смену
    # репозитория -- cont=False, new_origin назван.
    fake = Fake({"spec": {"meta": meta},
                 "update": {"bootstrap_marker": "m3"}})
    fake.answers["bootstrap_result"] = {"result": {"sent": "m3", "played": True,
                                                   "ok": True}}
    v = ops.update(fake, "pu-x-1", origin="git@h:g/new.git", workspace=("t", None),
                   login="ermak", force=True, sleep=nosleep, clock=stuck_clock)
    fields = fake.calls[1][1]
    c.check("origin switch forces a fresh clone",
            fields.get("cont") is False and fields.get("new_origin") == "git@h:g/new.git",
            fields)
    c.check("update carries owner and force", fields.get("owner") == "ermak"
            and fields.get("force") is True, fields)
    c.check("update success -> registered", v["outcome"] == "registered", v)

    # Спеки нет / не папет -> refused.
    fake = Fake({"spec": {"error": "no job pu-x-1"}})
    v = ops.update(fake, "pu-x-1", sleep=nosleep, clock=stuck_clock)
    c.check("no spec -> refused", v["outcome"] == "refused", v)

    # ── restart ─────────────────────────────────────────────────────────
    fake = Fake({"restart": {"ok": True, "owner_note": "held by ermak, forced"}})
    v = ops.restart(fake, "pu-x-1", force=True, login="ermak")
    c.check("restart accepted with the owner note",
            v["outcome"] == "accepted" and v.get("owner_note") == "held by ermak, forced", v)
    def refusing(verb, **fields):
        raise bus.Refused("held by someone else")
    v = ops.restart(refusing, "pu-x-1", login="ermak")
    c.check("restart refusal -> refused with the reason",
            v["outcome"] == "refused" and "held by" in v["error"], v)

    # ── restart: наблюдение рестарта (#464) ─────────────────────────────
    # HYPOTHESIS: ops.restart отвечает accepted сразу после RPC, не глядя,
    # перезапустилась ли задача: прежний running читается как успех, а тихий
    # провал рестарта веб-воркер (#476) разносит как сработавший.
    # SOLUTION: явный режим observe у той же операции: снимок сводки задачи
    # (счётчик рестартов, состояние -- глагол alloc несёт её с #126) ДО RPC,
    # затем опрос того же read-глагола, пока счётчик не сдвинется (или
    # задача не уйдёт из running / не встанет в ожидание рестарта), и факт
    # работоспособности -- снова running и не failed. Смена allocation ID --
    # НЕ подтверждение (тикет: «перезапуск задачи, а не новую аллокацию»):
    # это unknown. Без observe -- прежний контракт байт в байт.
    def alloc_raw(aid, state, restarts, failed=False, reason=None):
        return {"alloc": {"ID": aid, "ClientStatus": "running",
                          "task": {"state": state, "restarts": restarts,
                                   "exit": None, "next_s": None,
                                   "failed": failed},
                          "reason": reason}}

    def ticking(start=1_000_000.0, tick=10):
        t = [start]
        return lambda: (t.append(t.pop(0) + tick), t[0])[1]

    # Успех: тот же alloc ID, счётчик рестартов сдвинулся, задача снова
    # running -> registered с фактом.
    fake = Fake({"restart": {"ok": True, "owner_note": "held by ermak, forced"},
                 "alloc": [alloc_raw("a1", "running", 2),
                           alloc_raw("a1", "pending", 3),
                           alloc_raw("a1", "running", 3)]})
    v = ops.restart(fake, "pu-x-1", force=True, login="ermak", observe=True,
                    sleep=nosleep, clock=ticking())
    c.check("#464 the observed restart confirms the same allocation",
            v["outcome"] == "registered" and v.get("restarts") == 3
            and v.get("alloc") == "a1", v)
    verbs = [verb for verb, _ in fake.calls]
    c.check("#464 the snapshot precedes the RPC",
            verbs[0] == "alloc" and verbs[1] == "restart", verbs)
    c.check("#464 observation reads only",
            set(verbs) == {"alloc", "restart"}, verbs)

    # Прежний running -- не успех: счётчик стоит, состояние running --
    # ждём до срока и отвечаем unknown, не registered.
    fake = Fake({"restart": {"ok": True},
                 "alloc": alloc_raw("a1", "running", 2)})
    v = ops.restart(fake, "pu-x-1", login="ermak", observe=True,
                    sleep=nosleep, clock=ticking())
    c.check("#464 an old running is not a success: unknown",
            v["outcome"] == "unknown" and "restart" in (v.get("error") or ""), v)

    # Частичный сбой: рестарт увидели, задача вернулась падшей -> partial
    # с причиной (alloc несёт reason у падающих, #126).
    fake = Fake({"restart": {"ok": True},
                 "alloc": [alloc_raw("a1", "running", 2),
                           alloc_raw("a1", "pending", 3),
                           alloc_raw("a1", "dead", 3, failed=True,
                                     reason="oom killed")]})
    v = ops.restart(fake, "pu-x-1", observe=True, sleep=nosleep,
                    clock=ticking())
    c.check("#464 a task dead after the restart is partial with the reason",
            v["outcome"] == "partial" and "oom" in (v.get("error") or ""), v)

    # Увидели сигнал, но running так и не дождались -> partial по сроку
    # (эффект был, наблюдение не сошлось). Первый ответ уходит на снимок
    # ДО RPC, второй -- сигнал фазы 1, дальше -- ожидание в фазе 2.
    fake = Fake({"restart": {"ok": True},
                 "alloc": [alloc_raw("a1", "running", 2),
                           alloc_raw("a1", "pending", 3),
                           alloc_raw("a1", "pending", 3)]})
    v = ops.restart(fake, "pu-x-1", observe=True, observe_wait=1,
                    sleep=nosleep, clock=ticking(tick=5))
    c.check("#464 a seen restart without running back is partial",
            v["outcome"] == "partial" and v.get("restarts") == 3, v)

    # Новая аллокация -- НЕ подтверждение рестарта задачи (заголовок
    # тикета): id сменился под наблюдением -> unknown, оба id названы.
    fake = Fake({"restart": {"ok": True},
                 "alloc": [alloc_raw("a1", "running", 2),
                           alloc_raw("a2", "running", 0)]})
    v = ops.restart(fake, "pu-x-1", observe=True, sleep=nosleep,
                    clock=ticking())
    c.check("#464 a replaced allocation is not a confirmed restart",
            v["outcome"] == "unknown" and "a1" in (v.get("error") or "")
            and "a2" in (v.get("error") or ""), v)

    # Конкурентный send: наблюдение само читает, ничего не шлёт -- и чужой
    # send не читается сигналом (счётчик стоит -> unknown).
    fake = Fake({"restart": {"ok": True},
                 "alloc": alloc_raw("a1", "running", 2)})
    fake.node("node-1", "send", name="pu-x-1", message="hi")
    v = ops.restart(fake, "pu-x-1", observe=True, sleep=nosleep,
                    clock=ticking())
    c.check("#464 the observation itself sends nothing",
            not any(isinstance(call, tuple) and len(call) == 2
                    and call[0] == "send" for call in fake.calls), fake.calls)
    c.check("#464 a concurrent send is not a restart signal",
            v["outcome"] == "unknown", v)

    # Шина легла посреди наблюдения -> unknown, не failed: об эффекте мы
    # не узнали, а отказом это не было.
    class Dying(Fake):
        def __call__(self, verb, **fields):
            self.calls.append((verb, fields))
            if verb == "alloc" and len(self.calls) > 1:
                raise RuntimeError("bus down")
            return super().__call__(verb, **fields)
    v = ops.restart(Dying({"restart": {"ok": True},
                           "alloc": alloc_raw("a1", "running", 2)}),
                    "pu-x-1", observe=True, sleep=nosleep, clock=ticking())
    c.check("#464 a bus death mid-observation is unknown",
            v["outcome"] == "unknown" and "bus down" in (v.get("error") or ""), v)

    # Снимок не удался (read упал ДО RPC) -- эффект не блокируется:
    # рестарт уходит, наблюдение честно заканчивается unknown.
    fake = Fake({"restart": {"ok": True}})
    v = ops.restart(fake, "pu-x-1", observe=True, observe_wait=0,
                    sleep=nosleep, clock=ticking())
    c.check("#464 a failed pre-snapshot does not block the effect",
            [verb for verb, _ in fake.calls][:2] == ["alloc", "restart"]
            and v["outcome"] == "unknown", (v, fake.calls[:2]))

    # Отказ RPC при observe -- тот же refused, наблюдения нет.
    v = ops.restart(refusing, "pu-x-1", login="ermak", observe=True,
                    sleep=nosleep, clock=ticking())
    c.check("#464 a refused RPC stays refused under observe",
            v["outcome"] == "refused", v)

    # CLI-совместимость: без observe -- ни одного лишнего глагола, тот же
    # accepted с owner_note.
    fake = Fake({"restart": {"ok": True, "owner_note": "held by ermak, forced"}})
    v = ops.restart(fake, "pu-x-1", force=True, login="ermak")
    c.check("#464 the default mode keeps the old contract",
            v["outcome"] == "accepted"
            and v.get("owner_note") == "held by ermak, forced"
            and [verb for verb, _ in fake.calls] == ["restart"], (v, fake.calls))

    # Донаблюдение восстановления (#476): прерванное наблюдение доводит
    # observe_restart по ЖУРНАЛЬНОЙ базе (before), не переигрывая эффект.
    fake = Fake({"alloc": [alloc_raw("a1", "pending", 3),
                           alloc_raw("a1", "running", 3)]})
    v = ops.observe_restart(fake, "pu-x-1",
                            alloc_raw("a1", "running", 2)["alloc"],
                            sleep=nosleep, clock=ticking())
    c.check("#464 recovery observes the journal's baseline without replay",
            v["outcome"] == "registered" and v.get("restarts") == 3
            and [verb for verb, _ in fake.calls] == ["alloc", "alloc"], (v, fake.calls))

    # ── bootstrap по метке: порядок ожидания ────────────────────────────
    fake = Fake({"bootstrap_result": [{"result": {"sent": "other"}},
                                       {"result": {"sent": "other"}},
                                       {"result": {"sent": "mine", "played": True,
                                                   "ok": True}}]})
    sleeps = []
    got = ops.bootstrap_result(fake, "pu-x-1", "mine", 10,
                               sleep=lambda s: sleeps.append(s), clock=stuck_clock,
                               step=lambda s: None)
    c.check("bootstrap_result waits for ITS marker past foreign ones",
            got.get("sent") == "mine" and len(sleeps) == 2, (got, sleeps))

    # ── send_to_puppet: из client/channel.py, тонкой обёрткой ───────────
    fake = Fake({"send": {"msg_id": "id-1", "idle": {"state": "free (dev)"}}})
    v = ops.send_to_puppet(fake.node, "node-1", "pu-x-1", "hello", wait=3)
    node, verb, fields = fake.calls[0]
    c.check("send goes to the node agent with clamped wait",
            (node, verb) == ("node-1", "send") and fields.get("wait") == 3
            and fields.get("timeout") == 3 + bus.TIMEOUT, fake.calls[0])
    c.check("send verdict carries msg_id and idle",
            v.get("msg_id") == "id-1" and v.get("idle", {}).get("state") == "free (dev)", v)
    fake = Fake({"send": {"error": "held by ermak"}})
    v = ops.send_to_puppet(fake.node, "node-1", "pu-x-1", "hello")
    c.check("send refusal lands in the verdict", v.get("error") == "held by ermak", v)
    c.check("wait is clamped at the channel ceiling",
            ops._clamp_wait(10_000) == 600 and ops._clamp_wait(-5) == 0)

    # ── прогресс: список строк, мутируемый снаружи без вреда вердикту ────
    progress = ["step"]
    v = ops.outcome(ops.ACCEPTED, None, progress)
    progress.append("mutated")
    c.check("progress is copied into the verdict", v["progress"] == ["step"], v)

    return c.report("ops")


if __name__ == "__main__":
    sys.exit(main())
