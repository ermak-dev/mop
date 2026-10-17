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
import subprocess
import sys
import tempfile
from unittest.mock import patch

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched_env  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.cli.dev.web import build  # noqa: E402


def tree(root, files):
    for rel, data in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)


# ── #302: сборка страницы без библиотеки шины ─────────────────────────
# HYPOTHESIS: задача web:dist в образе node падает «bus library required»:
# пакет mop.cli.dev импортирует mop.cli.lib, а тот на верхнем уровне --
# mop.common.bus, которому нужен nats; в образе node его нет.
# SOLUTION: mop.cli.dev, mop.cli.dev.web и build берут lib только внутри
# функций, отказы сборки -- своей строкой. Проверка -- в чистом процессе:
# импорт build не заводит mop.common.bus. STATUS: FIXED — see #302
ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def check_no_bus_302(c):
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); "
             "import mop.cli.dev.web.build; "
             "print(sorted(m for m in sys.modules if m in ('mop.common.bus', 'mop.cli.lib', 'nats')))")
    got = subprocess.run([sys.executable, "-c", probe, ROOT], capture_output=True, text=True)
    c.check("#302 the web build imports without the bus",
            got.returncode == 0 and got.stdout.strip() == "[]", (got.stdout.strip(), got.stderr[-300:]))


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
    check_no_bus_302(c)
    # HYPOTHESIS: прямой вызов vite обходит prebuild и проверки store (#627).
    # SOLUTION: явный npm run test:store перед сборкой, без зависимости
    # от prebuild hooks; старый общий typecheck остаётся отдельным gate.
    # RESULT: red 3/9 подтвердил bypass, отказ store останавливает build.
    # STATUS: FIXED — see #627
    calls = []
    # HYPOTHESIS: npm ignore-scripts пропускает prebuild, оставляя Vite
    # зелёным без behavioral/typecheck store. Gate должен быть явным шагом.
    with patched_env(npm_config_ignore_scripts="true"), \
            patch.object(build, "tool", side_effect=lambda name: name), \
            patch.object(build, "run", side_effect=lambda argv, cwd: calls.append((argv, cwd))):
        build.build("/tmp/store-gate-output")
    c.expect("#627 store checks are explicit even with ignored lifecycle hooks", calls, [
        (["npm", "ci", "--no-audit", "--no-fund", "--loglevel=error"], build.WEB),
        (["npm", "run", "test:store"], build.WEB),
        (["npm", "run", "build:dist", "--", "--outDir", "/tmp/store-gate-output", "--emptyOutDir"], build.WEB),
    ])
    calls = []

    def failed_store(argv, cwd):
        calls.append(argv)
        if argv == ["npm", "run", "test:store"]:
            raise SystemExit(1)

    with patch.object(build, "tool", side_effect=lambda name: name), \
            patch.object(build, "run", side_effect=failed_store):
        try:
            build.build("/tmp/store-gate-output")
        except SystemExit as error:
            c.expect("#627 failed store gate fails the build", error.code, 1)
        else:
            c.fail("#627 failed store gate fails the build", "store gate was bypassed")
    c.expect("#627 Vite cannot run after a failed store gate", calls, [
        ["npm", "ci", "--no-audit", "--no-fund", "--loglevel=error"],
        ["npm", "run", "test:store"],
    ])
    # HYPOTHESIS: упакованного зеркала нет (#627/#644): wheel не везёт
    # web/dist, а «второй dist», скопированный руками, разошёлся бы молча.
    # SOLUTION: одна команда mop dev web build пишет web/dist И зеркало
    # mop/server/controller/dist; --check сравнивает оба дерева с чистой
    # сборкой. mirror(from_dir, to_dir) -- чистая функция над файлами.
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "fresh"), os.path.join(d, "package", "dist")
        tree(src, {"index.html": b"<html>", "assets/x.js": b"js", "assets/y.css": b"css"})
        tree(os.path.join(d, "package"), {"dist/stale.txt": b"old"})
        build.mirror(src, dst)
        c.expect("#627 mirror reproduces the fresh build byte for byte",
                 build.diff_trees(src, dst), [])
        # Зеркало перезаписывается целиком: устаревший файл не выживает.
        c.expect("#627 a stale mirror file is gone after the rebuild",
                 "stale.txt" not in build.files_of(dst), True)
        with open(os.path.join(dst, "index.html"), "rb") as f:
            c.expect("#627 mirror bytes equal the fresh build", f.read(), b"<html>")
    return c.report("webbuild")


if __name__ == "__main__":
    sys.exit(main())
