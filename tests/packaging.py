#!/usr/bin/env python3
"""Упаковка клиента без пула и без сети: python3 tests/packaging.py

Клиент ставится пакетом из git одной командой (#351):
`uv tool install git+ssh://<origin>/ermak/mop.git`. Здесь проверяется то,
что читается без сборки: pyproject.toml (tomllib, stdlib), зависимости,
которые отдаёт setup.py, и то, что мастер, поднятый из пакета, находит свой
MCP-сервер и скилл. Сама установка из git -- только руками.
"""
import importlib.util
import os
import sys
import tomllib

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, ROOT)

from mop.common import deps  # noqa: E402

PYPROJECT = os.path.join(ROOT, "pyproject.toml")
SETUP = os.path.join(ROOT, "setup.py")


def load_setup():
    """setup.py модулем, без setuptools: он импортируется только под
    __main__, а здесь нужна лишь функция requirements()."""
    spec = importlib.util.spec_from_file_location("mop_setup_py", SETUP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── #351: зависимости пакета -- тот же единственный список deps.PIP ─────
# HYPOTHESIS: упаковки нет вовсе; заведи её списком в pyproject -- и это
# второй список рядом с deps.PIP, который плейбуки раздают узлам, телам и
# CI: новая библиотека доехала бы до одних и молча не доехала до клиента.
# SOLUTION: pyproject объявляет dependencies динамическими, setup.py отдаёт
# install_requires = deps.PIP (exec файла, как в .gitlab-ci.yml). Сторож:
# статического списка нет, dependencies в dynamic, requirements() ==
# deps.PIP, вход mop -> mop.cli:console.
# STATUS: FIXED — see #351
def check_dependencies_351(c):
    if not c.check("#351 pyproject.toml exists", os.path.exists(PYPROJECT)):
        return
    with open(PYPROJECT, "rb") as f:
        meta = tomllib.load(f)
    project = meta.get("project", {})
    c.check("#351 no static dependency list in pyproject: deps.PIP is the one list",
            "dependencies" not in project)
    c.check("#351 dependencies are dynamic", "dependencies" in project.get("dynamic", []))
    c.expect("#351 the entry point", project.get("scripts"), {"mop": "mop.cli:console"})
    c.check("#351 the version is static", bool(project.get("version")))
    backend = meta.get("build-system", {}).get("build-backend")
    c.expect("#351 the backend runs setup.py", backend, "setuptools.build_meta")
    if not c.check("#351 setup.py exists", os.path.exists(SETUP)):
        return
    c.expect("#351 setup.py requirements == deps.PIP",
             load_setup().requirements(), list(deps.PIP))
    # Пакеты -- явно mop*, иначе flat-layout подобрал бы deploy/ и tests/.
    find = meta.get("tool", {}).get("setuptools", {}).get("packages", {}).get("find", {})
    c.expect("#351 packages: mop and below only", find.get("include"), ["mop", "mop.*"])
    data = meta.get("tool", {}).get("setuptools", {}).get("package-data", {})
    c.check("#351 the master skill ships as package data",
            "skills/master/*" in data.get("mop", []))


def check_console_351(c):
    from mop import cli
    c.check("#351 mop.cli.console: the entry point without arguments",
            callable(getattr(cli, "console", None)))


# ── #351: мастер из пакета находит свой MCP-сервер и скилл ─────────────
# HYPOTHESIS: master.py берёт MCP-команду как <PROJECT>/bin/mop, а скилл --
# <PROJECT>/skills/master. У пакета PROJECT -- site-packages: ни bin/, ни
# skills/ там нет, мастер-сессия встаёт без инструментов и без /master.
# SOLUTION: mcp_command(bin_dir, python) -- bin/mop клона, если он есть (MCP
# остаётся на дереве той копии, из которой подняли мастера), иначе
# [python, -m, mop.cli, mcp]; скилл переехал в mop/skills/master и едет в
# пакете, на старом месте -- переходный симлинк.
# STATUS: FIXED — see #351
def check_master_mcp_351(c):
    import tempfile
    from mop.cli.core import master
    fn = getattr(master, "mcp_command", None)
    if not c.check("#351 master.mcp_command exists", fn is not None):
        return
    with tempfile.TemporaryDirectory() as d:
        clone = os.path.join(d, "bin")
        os.makedirs(clone)
        launcher = os.path.join(clone, "mop")
        open(launcher, "w").close()
        c.expect("#351 a clone: its own bin/mop",
                 fn(clone, "/venv/bin/python"), (launcher, ["mcp"]))
        c.expect("#351 a package: the interpreter that runs mop",
                 fn(os.path.join(d, "nowhere"), "/venv/bin/python"),
                 ("/venv/bin/python", ["-m", "mop.cli", "mcp"]))
    got = master.mcp_config()
    c.check("#351 mcp_config carries mcp_command", '"mcp"' in got)


def check_master_skill_351(c):
    from mop.cli.core import master
    pkg = os.path.join(ROOT, "mop")
    c.expect("#351 the skill lives inside the package",
             os.path.realpath(master.SKILL), os.path.join(pkg, "skills", "master"))
    c.check("#351 the skill file is there",
            os.path.isfile(os.path.join(master.SKILL, "SKILL.md")))
    old = os.path.join(ROOT, "skills", "master")
    c.check("#351 the old path is a transition symlink",
            os.path.islink(old) and os.path.realpath(old) == os.path.join(pkg, "skills", "master"))


def main():
    c = Checks()
    check_dependencies_351(c)
    check_console_351(c)
    check_master_mcp_351(c)
    check_master_skill_351(c)
    return c.report("packaging")


if __name__ == "__main__":
    sys.exit(main())
