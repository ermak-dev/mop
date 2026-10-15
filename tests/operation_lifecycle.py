#!/usr/bin/env python3
"""Переходы устойчивой операции без БД и пула (#625).

HYPOTHESIS: прежний web lifecycle не различает финальный ответ клиенту и
возможный живой эффект; unknown/timeout ошибочно освобождают ресурс.
SOLUTION: явная таблица переходов и отдельный факт незавершённого эффекта;
проверка ревизии остаётся обязательной даже для разрешённого перехода.
RESULT: таблица переходов и отказ освобождать возможный живой эффект проверены;
конкурентные CAS/claim/outbox проверяются отдельно на PostgreSQL, не здесь.
STATUS: FIXED — see #625
"""
import importlib
import importlib.util
import os
import sys

import hermetic  # noqa: F401
from _lib import Checks

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


ALLOWED = {
    "accepted": {"running", "refused", "expired", "cancelled"},
    "running": {"succeeded", "refused", "failed", "partial", "reconciling"},
    "reconciling": {"succeeded", "failed", "partial", "unknown"},
    "unknown": {"reconciling"},
    "succeeded": set(),
    "refused": set(),
    "failed": set(),
    "partial": set(),
    "expired": set(),
    "cancelled": set(),
}


def rejects(c, what, fn, phrase):
    try:
        fn()
    except ValueError as error:
        c.check(what, phrase in str(error), str(error))
    else:
        c.fail(what, "transition was accepted")


def main():
    c = Checks()
    try:
        present = importlib.util.find_spec("mop.server.operations.lifecycle") is not None
    except ModuleNotFoundError:
        present = False
    if not c.check("durable operation lifecycle API is available", present):
        return c.report("operation_lifecycle")
    lifecycle = importlib.import_module("mop.server.operations.lifecycle")

    c.expect("the finite state vocabulary", {state.value for state in lifecycle.State}, set(ALLOWED))
    for before, afters in ALLOWED.items():
        for after in ALLOWED:
            if after in afters:
                got = lifecycle.transition(before, after, revision=7, expected_revision=7)
                c.expect(f"{before} -> {after}", got.state.value, after)
                c.expect(f"{before} -> {after} advances once", got.revision, 8)
            else:
                rejects(c, f"{before} -> {after} is not a transition",
                        lambda b=before, a=after: lifecycle.transition(
                            b, a, revision=7, expected_revision=7), "transition")

    rejects(c, "an otherwise valid stale transition refuses",
            lambda: lifecycle.transition("accepted", "running", revision=8, expected_revision=7),
            "revision")
    for bad in (True, False, 0, -1, 1.5, "7", None):
        rejects(c, f"invalid current revision {bad!r}",
                lambda bad=bad: lifecycle.transition("accepted", "running", revision=bad,
                                                   expected_revision=7), "revision")
        rejects(c, f"invalid expected revision {bad!r}",
                lambda bad=bad: lifecycle.transition("accepted", "running", revision=7,
                                                   expected_revision=bad), "revision")
    for bad in ("waiting", "", None, 1, True):
        rejects(c, f"unknown source state {bad!r}",
                lambda bad=bad: lifecycle.transition(bad, "running", revision=7,
                                                   expected_revision=7), "state")
        rejects(c, f"unknown target state {bad!r}",
                lambda bad=bad: lifecycle.transition("accepted", bad, revision=7,
                                                   expected_revision=7), "state")

    for state in ALLOWED:
        c.expect(f"{state} cannot release a possibly live effect",
                 lifecycle.may_release_resources(state, effect_quiescent=False), False)
        c.expect(f"{state} releases only after a releasable state and quiescence",
                 lifecycle.may_release_resources(state, effect_quiescent=True),
                 state in {"succeeded", "refused", "failed", "partial", "expired", "cancelled", "unknown"})
    for bad in (None, 0, 1, "false"):
        rejects(c, f"ambiguous effect quiescence {bad!r}",
                lambda bad=bad: lifecycle.may_release_resources("succeeded", effect_quiescent=bad),
                "quiescent")
    rejects(c, "unknown outcome is not a resource-release state",
            lambda: lifecycle.may_release_resources("legacy-finished", effect_quiescent=True),
            "state")
    c.expect("a timed-out running effect cannot release its lane",
             lifecycle.may_release_resources("running", effect_quiescent=False), False)
    c.expect("a settled unknown response cannot release an unresolved lane",
             lifecycle.may_release_resources("unknown", effect_quiescent=False), False)
    # HYPOTHESIS: требование известного исхода держит ресурс навсегда,
    # даже когда отдельная сверка доказала окончание физического эффекта.
    # SOLUTION: unknown сохраняет честный исход, но не запрещает release
    # при подтверждённой quiescence; таймаут по-прежнему не доказательство.
    c.expect("confirmed quiescence releases without fabricating a known outcome",
             lifecycle.may_release_resources("unknown", effect_quiescent=True), True)

    return c.report("operation_lifecycle")


if __name__ == "__main__":
    sys.exit(main())
