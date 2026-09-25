#!/usr/bin/env python3
"""Проверка раздачи файлов на узлы без пула: python3 tests/keys.py

Раздача (`mop login`, ключи LLM у `mop add`) -- глагол `write` агентам всех
узлов разом. Здесь -- как ответы агентов становятся результатом по узлам;
сама запись -- только на живом пуле.
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import bus  # noqa: E402
from mop.client import keys  # noqa: E402


def main():
    c = Checks()
    # HYPOTHESIS (#135): неответ агента уводил в запасной путь через
    # sysbatch Nomad, а без токена печатал «agent silent … run mop login on
    # the controller» -- при том что агент просто писал в тела дольше
    # таймаута. SOLUTION: запасного пути нет; неответ -- отказ с причиной.
    # STATUS: FIXED — see #135
    fn = getattr(keys, "results_from", None)
    if c.check("keys.results_from exists", fn is not None):
        got = fn(["a", "b", "c", "d"], {
            "a": {"written": ["/x"]},
            "b": {"error": "agent is not allowed to write to /etc/passwd"},
            "c": bus.BusError("node agent c did not answer in 20s"),
        })
        c.check("a written node must be OK", not (got.get("a") != "OK"), got)
        c.check("an agent's refusal must be FAILED with its reason",
                not (not got.get("b", "").startswith("FAILED") or "not allowed" not in got["b"]),
                got)
        c.check("a silent agent must say so, not send to the controller",
                not ("did not answer in 20s" not in got.get("c", "")
                     or "Nomad" in got.get("c", "")), got)
        c.check("a node with no answer at all must be NOT REACHED",
                not (not got.get("d", "").startswith("NOT REACHED")), got)
    for gone in ("_push_via_sysbatch", "push_spec", "push_script", "LOGIN_JOB"):
        c.check(f"keys.{gone} must be gone: there is no fallback past the agent",
                not hasattr(keys, gone))
    return c.report("keys")


if __name__ == "__main__":
    sys.exit(main())
