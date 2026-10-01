#!/usr/bin/env python3
"""Раздача ключа прокси без пула: python3 tests/proxykey.py

Сервер -- единственный раздатчик ключа LLM-прокси (#391): рендер блоба и
решение «пора ли снова» -- чистые функции, сама раздача -- только на живом
пуле. Здесь же итоги глагола write (bus.results_from): клиентский
раздатчик keys.py умер (#385), потребитель один -- этот модуль.
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import bus  # noqa: E402
from mop.server import proxykey  # noqa: E402


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
    for rel in ("mop/server/proxykey.py", "mop/node/agent.py"):
        src = open(os.path.join(root, rel)).read()
        tree = ast.parse(src)
        own = sorted({n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                      and n.name in ("results_from", "_as_file", "as_file")}
                     | {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                        for t in n.targets if isinstance(t, ast.Name) and t.id == "WRITE_TIMEOUT"})
        c.check(f"{rel}: no own copy of the write helpers", not own, own)
        c.check(f"{rel}: no base64 of its own, the encoding lives in bus", "base64" not in src)
    for rel in ("mop/server/proxykey.py",):
        # Строка-результат, а не упоминание в докстринге: f"NOT REACHED: …".
        tree = ast.parse(open(os.path.join(root, rel)).read())
        built = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
                 and any(isinstance(v, ast.Constant) and "NOT REACHED" in str(v.value)
                         for v in n.values)]
        c.check(f"{rel}: no answer mapping by hand (NOT REACHED lives in bus)", not built, built)


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


def check_results_135(c):
    """Итоги write (#135): неответ -- отказ с причиной, запасного пути нет."""
    fn = getattr(bus, "results_from", None)
    if not c.check("bus.results_from exists", fn is not None):
        return
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
            not ("did not answer in 20s" not in got.get("c")
                 or "Nomad" in got.get("c")), got)
    c.check("a node with no answer at all must be NOT REACHED",
            not (not got.get("d", "").startswith("NOT REACHED")))

def main():
    c = Checks()
    # ── блоб secrets.env (#391) ─────────────────────────────────────────
    # HYPOTHESIS: клиентская раздача (мастер-шелл, mop login) умерла, и узлы
    # не получают MOP_PROXY_KEY -- свежие тела сеются со старой копии узла и
    # врапер умирает «no key». SOLUTION: рендерит и раздаёт сервер.
    # С #390 в блобе ровно один ключ: переходный Z_AI_KEY снят вместе с
    # профилями -- glm-папеты перерегистрируются на прокси тем же ходом.
    # STATUS: FIXED — see #391, #390
    c.expect("blob: the proxy key rides (#391)",
             proxykey.blob({"MOP_PROXY_KEY": "s1", "Z_AI_KEY": "z1"}),
             "MOP_PROXY_KEY=s1\n")
    c.expect("blob: no key at all -> nothing to ride (#391)",
             proxykey.blob({"Z_AI_KEY": "z1"}), None)
    c.expect("blob: an empty key is no key (#391)",
             proxykey.blob({"MOP_PROXY_KEY": ""}), None)

    # ── пора ли раздавать (#391): по отпечатку, недошедшие не считаются ──
    c.expect("due: no state -> push", proxykey.due("b", None), True)
    c.expect("due: same sha -> skip", proxykey.due("b", {"sha": proxykey.sha("b")}), False)
    c.expect("due: changed blob -> push",
             proxykey.due("b2", {"sha": proxykey.sha("b")}), True)
    c.check("sha: отличает блобы", proxykey.sha("a") != proxykey.sha("b"))
    c.check("settled: недошедший узел держит раздачу открытой (#391)",
            not proxykey.settled({"gpu": "OK", "hyper": "NOT REACHED: down"}))
    c.check("settled: отказ агента раздачу не держит (#391)",
            proxykey.settled({"gpu": "OK", "hyper": "FAILED: x"}))
    c.check("settled: пустой итог -- дошли", proxykey.settled({}))

    check_results_135(c)
    check_write_in_bus_315(c)
    check_body_refusal_366(c)
    return c.report("proxykey")


if __name__ == "__main__":
    sys.exit(main())
