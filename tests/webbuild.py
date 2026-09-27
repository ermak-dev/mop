#!/usr/bin/env python3
"""Сборка дашборда без node: python3 tests/webbuild.py

`mop dev web build --check` (#297) собирает приложение во временный каталог
и сравнивает с закоммиченным web/dist байт в байт: собранный бандл лежит в
репозитории, потому что на серверах node нет и rumop раскатывается с
сервера вручную, а разошедшийся с исходниками dist молчал бы. Сравнение
деревьев -- чистая функция, она проверяется здесь; сама сборка -- CI.

HYPOTHESIS: командлета нет, dist собирается руками и расходится молча.
SOLUTION: diff_trees(a, b) -> отличающиеся относительные пути (нет в одном
из деревьев или другие байты), пусто -- деревья равны.
STATUS: FIXED — see #297
"""
import os
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.cli.dev.web import build  # noqa: E402


def tree(root, files):
    for rel, data in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)


def main():
    c = Checks()
    with tempfile.TemporaryDirectory() as d:
        a, b = os.path.join(d, "a"), os.path.join(d, "b")
        tree(a, {"index.html": b"<html>", "assets/x.js": b"js", "assets/y.css": b"css"})
        tree(b, {"index.html": b"<html>", "assets/x.js": b"js", "assets/y.css": b"css"})
        c.expect("equal trees differ nowhere", build.diff_trees(a, b), [])
        tree(b, {"assets/x.js": b"JS"})
        c.expect("changed bytes are named", build.diff_trees(a, b), ["assets/x.js"])
        tree(a, {"assets/z.svg": b"svg"})
        c.expect("a file missing on one side is named", build.diff_trees(a, b),
                 ["assets/x.js", "assets/z.svg"])
        tree(b, {"assets/x.js": b"js", "assets/z.svg": b"svg", "assets/old.js": b"gone"})
        c.expect("a file only in the committed tree is named too", build.diff_trees(a, b),
                 ["assets/old.js"])
        c.expect("a missing tree is every file of the other",
                 build.diff_trees(a, os.path.join(d, "none")),
                 ["assets/x.js", "assets/y.css", "assets/z.svg", "index.html"])
    return c.report("webbuild")


if __name__ == "__main__":
    sys.exit(main())
