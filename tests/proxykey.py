#!/usr/bin/env python3
"""Раздача ключа прокси без пула: python3 tests/proxykey.py

Сервер -- единственный раздатчик ключа LLM-прокси (#391): рендер блоба и
решение «пора ли снова» -- чистые функции, сама раздача -- только на живом
пуле.
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.server import proxykey  # noqa: E402


def main():
    c = Checks()
    # ── блоб secrets.env (#391) ─────────────────────────────────────────
    # HYPOTHESIS: клиентская раздача (мастер-шелл, mop login) умерла, и узлы
    # не получают MOP_PROXY_KEY -- свежие тела сеются со старой копии узла и
    # врапер умирает «no key». SOLUTION: рендерит и раздаёт сервер; переходный
    # Z_AI_KEY едет рядом, пока есть в .env сервера (стендовые glm-папеты,
    # #382), и исчезает из блоба сам, как только его уберут из .env.
    # STATUS: FIXED — see #391
    c.expect("blob: the proxy key rides (#391)",
             proxykey.blob({"MOP_PROXY_KEY": "s1", "Z_AI_KEY": "z1"}),
             "MOP_PROXY_KEY=s1\nZ_AI_KEY=z1\n")
    c.expect("blob: no Z_AI_KEY in .env -> only the proxy key (#391)",
             proxykey.blob({"MOP_PROXY_KEY": "s1"}), "MOP_PROXY_KEY=s1\n")
    c.expect("blob: transition rides without the proxy key yet (#391)",
             proxykey.blob({"Z_AI_KEY": "z1"}), "Z_AI_KEY=z1\n")
    c.expect("blob: empty values are skipped (#391)",
             proxykey.blob({"MOP_PROXY_KEY": "", "Z_AI_KEY": ""}), None)

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
    return c.report("proxykey")


if __name__ == "__main__":
    sys.exit(main())
