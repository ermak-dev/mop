#!/usr/bin/env python3
"""Печать без кластера: python3 tests/render.py

HYPOTHESIS (#243): слоты печатались одним числом свободных -- «(0 slots)» в
подвале `mop list`, колонка SLOTS в `mop node`, «#слотов» на панели, -- и
сколько папетов берёт пустой узел, не видел никто.
SOLUTION: render.ratio(свободно, всего) -> «N/M» -- одна запись дроби на
фронтенды CLI; неизвестная часть -- «-», обе неизвестны -- «-». Страница
питон не импортирует: её ratio (web/src/components/format.ts) держится
той же записи, случаи сверяет format.test.ts.
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
    # Страница держится той же записи: своя ratio с прочерками (#243). С #301
    # страница -- React: ratio в components/format.ts, случаи те же, что
    # CASES, сверяет Vitest (format.test.ts); здесь -- что колонка слотов
    # идёт через неё одна.
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                       "web", "src", "components")
    fmt = open(os.path.join(src, "format.ts"), encoding="utf-8").read()
    panel = open(os.path.join(src, "NodesPanel.tsx"), encoding="utf-8").read()
    c.check("format.ts: the page's own ratio, N/M", "export function ratio" in fmt)
    c.check("NodesPanel: one слоты column through ratio",
            "ratio(n.slots, n.slots_total)" in panel and "#слотов" not in panel)
    return c.report("render")

if __name__ == "__main__":
    sys.exit(main())
