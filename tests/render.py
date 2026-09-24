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
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import render  # noqa: E402

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
    failed = []
    fn = getattr(render, "ratio", None)
    if fn is None:
        failed.append("render.ratio is missing")
    else:
        for args, want in CASES:
            got = fn(*args)
            if got != want:
                failed.append(f"ratio{args} -> {got!r}, want {want!r}")
    # Страница держится той же записи: своя ratio с прочерками.
    page = open(os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                             "web", "index.html")).read()
    if "const ratio" not in page or "n.slots_total" not in page or "#слотов" in page:
        failed.append("web/index.html: one слоты column through its own ratio, N/M")
    print("\n".join(f"FAIL {l}" for l in failed) if failed else "", end="\n" if failed else "")
    print("render: FAILED" if failed else "render: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
