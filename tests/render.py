#!/usr/bin/env python3
"""Печать без кластера: python3 tests/render.py

HYPOTHESIS (#243): слоты печатались одним числом свободных -- «(0 slots)» в
подвале `mop list`, колонка SLOTS в `mop node`, «#слотов» на панели, -- и
сколько папетов берёт пустой узел, не видел никто.
SOLUTION: render.ratio(свободно, всего) -> «N/M» -- одна запись дроби на
фронтенды CLI; неизвестная часть -- «-», обе неизвестны -- «-». Страница
питон не импортирует: её ratio в web/index.html держится той же записи.
STATUS: FIXED — see #243
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import render  # noqa: E402

CASES = [
    ((0, 5), "0/5"),
    ((3, 5), "3/5"),
    ((5, 5), "5/5"),
    ((0, 0), "0/0"),
    ((2, None), "2/-"),        # сервис кластера старше #243: всего не знает
    ((None, 5), "-/5"),
    ((None, None), "-"),       # узел не ready: ёмкости нет вовсе
]


def main():
    c = Checks()
    fn = getattr(render, "ratio", None)
    if c.check("render.ratio exists", fn is not None):
        for args, want in CASES:
            c.expect(f"ratio{args}", fn(*args), want)
    # Страница держится той же записи: своя ratio с прочерками.
    page = open(os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                             "web", "index.html")).read()
    c.check("web/index.html: one слоты column through its own ratio, N/M",
            "const ratio" in page and "n.slots_total" in page and "#слотов" not in page)
    return c.report("render")

if __name__ == "__main__":
    sys.exit(main())
