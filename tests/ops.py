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
