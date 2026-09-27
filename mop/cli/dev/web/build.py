"""mop dev web build [--check]: build the dashboard into web/dist

Runs `npm ci` and `vite build` in web/. Silent when the build lands.

  --check   build into a temporary directory instead and compare it with
            web/dist byte for byte; the differing files are listed and the
            exit code is 1 -- the committed dist must be rebuilt

Needs node and npm on this machine (the developer's or CI's), never on a
server.
"""
import os
import shutil
import subprocess
import sys
import tempfile

from mop.common import config
# Без mop.cli.lib (#302): lib на верхнем уровне импортирует шину, а задача
# web:dist идёт в образе node, где питонских библиотек нет. Отказ -- та же
# красная строка, что у lib: одна на двоих в mop.cli.term (#320).
from mop.cli.term import fail, usage

WEB = os.path.join(config.PROJECT, "web")
DIST = os.path.join(WEB, "dist")


def files_of(root):
    """{относительный путь: байты} дерева; нет каталога -- пусто."""
    out = {}
    for d, _, files in os.walk(root):
        for f in files:
            path = os.path.join(d, f)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root).replace(os.sep, "/")] = fh.read()
    return out


def diff_trees(a, b):
    """Отличающиеся относительные пути двух деревьев, по алфавиту: файл
    только в одном из них или с другими байтами. Пусто -- деревья равны."""
    fa, fb = files_of(a), files_of(b)
    return sorted(p for p in set(fa) | set(fb) if fa.get(p) != fb.get(p))


def tool(name):
    """Путь исполняемого файла или громкий отказ: сборка без node
    бессмысленна, и трасса «No such file» сказала бы меньше."""
    path = shutil.which(name)
    if not path:
        fail(f"{name} is not installed: the dashboard build needs node and npm")
        sys.exit(1)
    return path


def run(argv, cwd):
    """Команда тулчейна: вывод -- только при отказе."""
    got = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if got.returncode:
        sys.stderr.write(got.stdout + got.stderr)
        fail(f"{' '.join(os.path.basename(a) for a in argv[:2])} failed with exit {got.returncode}")
        sys.exit(1)


def build(out_dir):
    npm, npx = tool("npm"), tool("npx")
    run([npm, "ci", "--no-audit", "--no-fund", "--loglevel=error"], WEB)
    run([npx, "vite", "build", "--outDir", out_dir, "--emptyOutDir"], WEB)


def main(argv):
    if set(argv) - {"--check"}:
        usage(__doc__)
    if "--check" not in argv:
        build(DIST)
        return 0
    with tempfile.TemporaryDirectory(prefix="mop-web-dist-") as tmp:
        build(tmp)
        diff = diff_trees(tmp, DIST)
    if diff:
        fail("web/dist differs from a fresh build; rebuild it with mop dev web build:\n  "
                 + "\n  ".join(diff))
        return 1
    return 0
