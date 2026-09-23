#!/usr/bin/env python3
"""Проверка раздачи файлов на узлы без пула: python3 tests/keys.py

Раздача (`mop login`, ключи LLM у `mop add`) -- глагол `write` агентам всех
узлов разом. Здесь -- как ответы агентов становятся результатом по узлам;
сама запись -- только на живом пуле.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import bus, keys  # noqa: E402


def main():
    failed = []
    # HYPOTHESIS (#135): неответ агента уводил в запасной путь через
    # sysbatch Nomad, а без токена печатал «agent silent … run mop login on
    # the controller» -- при том что агент просто писал в тела дольше
    # таймаута. SOLUTION: запасного пути нет; неответ -- отказ с причиной.
    # STATUS: FIXED — see #135
    fn = getattr(keys, "results_from", None)
    if fn is None:
        failed.append("keys.results_from is missing")
    else:
        got = fn(["a", "b", "c", "d"], {
            "a": {"written": ["/x"]},
            "b": {"error": "agent is not allowed to write to /etc/passwd"},
            "c": bus.BusError("node agent c did not answer in 20s"),
        })
        if got.get("a") != "OK":
            failed.append(f"a written node must be OK: {got}")
        if not got.get("b", "").startswith("FAILED") or "not allowed" not in got["b"]:
            failed.append(f"an agent's refusal must be FAILED with its reason: {got}")
        if "did not answer in 20s" not in got.get("c", "") or "Nomad" in got.get("c", ""):
            failed.append(f"a silent agent must say so, not send to the controller: {got}")
        if not got.get("d", "").startswith("NOT REACHED"):
            failed.append(f"a node with no answer at all must be NOT REACHED: {got}")
    for gone in ("_push_via_sysbatch", "push_spec", "push_script", "LOGIN_JOB"):
        if hasattr(keys, gone):
            failed.append(f"keys.{gone} must be gone: there is no fallback past the agent")

    print("\n".join(f"FAIL {l}" for l in failed) if failed else "", end="\n" if failed else "")
    print("keys: FAILED" if failed else "keys: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
