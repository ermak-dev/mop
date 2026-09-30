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
    registered = {}

    def call_cluster(verb, timeout=None, project=None, **kw):
        registered["verb"], registered["fields"] = verb, kw
        return {"ok": True}
    saved = (bus.request_many, puppets.ready_nodes, keys.credentials, keys.llm_keys_blob,
             bus.call_cluster, bus.login)
    try:
        bus.request_many = request_many
        bus.call_cluster = call_cluster
        bus.login = lambda: "anton"
        puppets.ready_nodes = lambda: {"n1"}
        keys.credentials = lambda: b"{}"
        keys.llm_keys_blob = lambda: ("K=v\n", None)
        results, _what, _note = keys.push_login()
        c.expect("push_login: every node OK", results, {"n1": "OK"})
        got = sorted(f[0] for f in sent.get("files") or [])
        # Логин и ключи -- два имени из белого списка; метка кредита (#284)
        # в нём третья, но её кладёт сервер при раздаче кредита, не login.
        c.expect("push_login sends the relative names, no home",
                 got, sorted([paths.CREDENTIALS, paths.NODE_SECRETS]))
        # Переход к реестру (#284): тот же файл -- кредитом под именем логина.
        c.expect("push_login registers the login as a credential named after the bus login",
                 (registered.get("verb"), (registered.get("fields") or {}).get("name"),
                  (registered.get("fields") or {}).get("credentials")),
                 ("cred_add", "anton", "{}"))
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
        (bus.request_many, puppets.ready_nodes, keys.credentials, keys.llm_keys_blob,
         bus.call_cluster, bus.login) = saved


# ── #315: глагол write -- ответы, кодировка и таймаут в одном месте ─────
# HYPOTHESIS: results_from, кодировка файла [путь, b64] и WRITE_TIMEOUT
# держат копиями client/keys.py и server/credreg.py, декодер -- агент сам.
# SOLUTION: bus.results_from, bus.as_file / bus.file_data, bus.WRITE_TIMEOUT;
# вызывающие только переключились. STATUS: FIXED — see #315
def check_write_in_bus_315(c):
    import ast
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    for name in ("results_from", "as_file", "file_data", "WRITE_TIMEOUT"):
        c.check(f"bus.{name} exists", hasattr(bus, name))
    if hasattr(bus, "as_file") and hasattr(bus, "file_data"):
        for data in ("K=v\n", b"\x00\xffbin"):
            path, b64 = bus.as_file("p/q", data)
            raw = data.encode() if isinstance(data, str) else data
            c.expect(f"bus.as_file -> file_data round-trips {data!r}",
                     (path, bus.file_data(b64)), ("p/q", raw))
            c.check("bus.as_file: b64 is a str, it goes into JSON", isinstance(b64, str))
    c.expect("bus.WRITE_TIMEOUT stays 60s", getattr(bus, "WRITE_TIMEOUT", None), 60)
    for rel in ("mop/client/keys.py", "mop/server/credreg.py", "mop/node/agent.py"):
        src = open(os.path.join(root, rel)).read()
        tree = ast.parse(src)
        own = sorted({n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                      and n.name in ("results_from", "_as_file", "as_file")}
                     | {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                        for t in n.targets if isinstance(t, ast.Name) and t.id == "WRITE_TIMEOUT"})
        c.check(f"{rel}: no own copy of the write helpers", not own, own)
        c.check(f"{rel}: no base64 of its own, the encoding lives in bus", "base64" not in src)
    for rel in ("mop/client/keys.py", "mop/server/credreg.py"):
        # Строка-результат, а не упоминание в докстринге: f"NOT REACHED: …".
        tree = ast.parse(open(os.path.join(root, rel)).read())
        built = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
                 and any(isinstance(v, ast.Constant) and "NOT REACHED" in str(v.value)
                         for v in n.values)]
        c.check(f"{rel}: no answer mapping by hand (NOT REACHED lives in bus)", not built, built)
    src = open(os.path.join(root, "mop/server/credreg.py")).read()
    c.check("server/credreg.py: fan-out through bus.request_many, no bus.request loop",
            "bus.request(" not in src and "bus.request_many(" in src)


# ── #366: отказ тела в ответе write ─────────────────────────────────────
# HYPOTHESIS: агент кладёт отказ тела строкой в written, поля error нет;
# results_from решал только по error, и `mop login` печатал узел, где
# запись во все тела упала, как OK.
# SOLUTION: агент отдаёт failed {тело: причина} (#366, узловая половина);
# results_from считает узел FAILED с причиной первого тела, если failed
# непуст -- одно правило на клиента и сервер (#315).
# STATUS: FIXED — see #366
def check_body_refusal_366(c):
    got = bus.results_from(["n1", "n2", "n3"], {
        "n1": {"written": ["pu-a-1 FAILED — body 9001 is stopped",
                           "pu-a-2 FAILED — body 9002 is stopped"],
               "failed": {"pu-a-1": "body 9001 is stopped", "pu-a-2": "body 9002 is stopped"},
               "absent": []},
        "n2": {"written": ["pu-a-3:/x"], "failed": {}, "absent": []},
        # Формулировка строки -- не контракт: судит поле.
        "n3": {"written": ["pu-a-4 could not be written"],
               "failed": {"pu-a-4": "ssh: connection refused"}, "absent": []}})
    c.expect("#366 every body refused: FAILED with the first body's reason",
             got.get("n1"), "FAILED: pu-a-1: body 9001 is stopped")
    c.expect("#366 nothing refused: OK", got.get("n2"), "OK")
    c.expect("#366 the field decides, not the wording of written",
             got.get("n3"), "FAILED: pu-a-4: ssh: connection refused")


def main():
    c = Checks()
    # HYPOTHESIS (#135): неответ агента уводил в запасной путь через
    # sysbatch Nomad, а без токена печатал «agent silent … run mop login on
    # the controller» -- при том что агент просто писал в тела дольше
    # таймаута. SOLUTION: запасного пути нет; неответ -- отказ с причиной.
    # STATUS: FIXED — see #135
    # С #315 разбор живёт в bus -- рядом с verdict, на котором стоит.
    fn = getattr(bus, "results_from", None)
    if c.check("bus.results_from exists", fn is not None):
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
    check_write_in_bus_315(c)
    check_body_refusal_366(c)
    return c.report("keys")


if __name__ == "__main__":
    sys.exit(main())
