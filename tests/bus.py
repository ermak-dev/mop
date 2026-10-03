#!/usr/bin/env python3
"""Отказ на повреждённый ответ шины без сети: python3 tests/bus.py"""
import os
import sys
from types import SimpleNamespace

import hermetic  # noqa: F401,E402 -- изоляция раньше mop
from _lib import Checks, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import bus  # noqa: E402


def main():
    c = Checks()
    # HYPOTHESIS: диагностике достаточно причины и адресата; сырой ответ
    # удалённой службы иногда содержит закрытые данные и не должен выходить.
    # SOLUTION: отбрасывать тело неразобранного ответа на границе шины.
    # RESULT: оба варианта отказа не цитируют содержимое ответа.
    # STATUS: FIXED — see #435
    for payload in (b"SECRET_MARKER=example; not json", b"\xffSECRET_MARKER"):
        reply = SimpleNamespace(data=payload)
        with patched(bus, connect=lambda: object(),
                     arequest=lambda *args: object(),
                     _call=lambda *args: reply):
            try:
                bus._ask("mop.test.node.worker.rpc", "node worker", "no responders",
                         "state", 1)
                c.fail("#435 malformed reply must be refused")
            except bus.BusError as e:
                c.expect("#435 malformed reply names failure without contents", str(e),
                         "node worker returned an invalid response")
                c.check("#435 raw reply marker stays private", "SECRET_MARKER" not in str(e))
    return c.report("bus")


if __name__ == "__main__":
    sys.exit(main())
