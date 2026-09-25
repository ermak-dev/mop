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

from mop.common import bus, paths, puppets  # noqa: E402
from mop.client import keys  # noqa: E402


# ── дом пула не едет в глаголе write (#279) ──────────────────────────────
# HYPOTHESIS: push_login и push_llm_keys собирают путь из puppets.HOME --
# MOP_HOME установки оператора; на чужом сервере (rumop, дом /home/mop) агент
# отказывал по каждому узлу. SOLUTION: клиент шлёт имена из paths.WRITABLE,
# относительно дома; дом подставляет агент. STATUS: FIXED — see #279
def check_no_home_279(c):
    sent = {}

    def request_many(verb, nodes, timeout=None, **kw):
        sent["verb"], sent["files"] = verb, kw.get("files")
        return {n: {"written": []} for n in nodes}
    saved = (bus.request_many, puppets.ready_nodes, keys.credentials, keys.llm_keys_blob)
    try:
        bus.request_many = request_many
        puppets.ready_nodes = lambda: {"n1"}
        keys.credentials = lambda: b"{}"
        keys.llm_keys_blob = lambda: ("K=v\n", None)
        results, _what, _note = keys.push_login()
        c.expect("push_login: every node OK", results, {"n1": "OK"})
        got = sorted(f[0] for f in sent.get("files") or [])
        c.expect("push_login sends the relative names, no home",
                 got, sorted(paths.WRITABLE))
        for p in got:
            c.check(f"push_login: {p} is relative", not os.path.isabs(p))
        from mop.common import llm
        saved_get = llm.get
        try:
            llm.get = lambda _p: {"key": "K"}
            keys.push_llm_keys("prof")
        finally:
            llm.get = saved_get
        got = [f[0] for f in sent.get("files") or []]
        c.expect("push_llm_keys sends the relative secrets name", got,
                 [getattr(paths, "NODE_SECRETS", ".config/mop/secrets.env")])
    finally:
        bus.request_many, puppets.ready_nodes, keys.credentials, keys.llm_keys_blob = saved


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
    check_no_home_279(c)
    return c.report("keys")


if __name__ == "__main__":
    sys.exit(main())
