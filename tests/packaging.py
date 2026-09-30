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


# ── #368: MCP-инструменты и перезапуск --from-ci зовут себя и из пакета ──
# HYPOTHESIS: адаптер MCP зовёт командлеты как <cli.BIN>/mop, а ci_pull
# перезапускает себя как <lib.BIN>/mop deploy --from-ci. У пакета bin/ нет:
# каждый инструмент-командлет отдаёт модели [Errno 2], а перезапуск падает;
# вдобавок ci_pull зовёт прежнее имя из LEGACY, которое уйдёт.
# SOLUTION: lib.self_argv(bin_dir, python) по правилу #351 -- bin/mop клона,
# если он есть, иначе [python, -m, mop.cli]; на ней стоят mcp_command,
# mcp.run_command и ci_pull (каноническое `server deploy --from-ci`).
# STATUS: FIXED — see #368
def check_self_argv_368(c):
    import tempfile
    from _lib import patched
    from mop.cli import lib
    fn = getattr(lib, "self_argv", None)
    if not c.check("#368 lib.self_argv exists", fn is not None):
        return
    from mop.cli.server import deploy
    from mop.cli.service import mcp
    from mop.client import channel
    py = "/venv/bin/python"

    def adapter_argv():
        calls = []

        class Done:
            returncode, stdout, stderr = 0, "ok", ""

        def run(argv, **kw):
            calls.append(argv)
            return Done()
        with patched(mcp.subprocess, run=run), \
                patched(channel, my_session=lambda: {"cwd": d}):
            mcp.run_command(["list"], ["--all"])
        return calls[0] if calls else None

    def restart_argv():
        calls = []

        def execv(path, argv):
            calls.append((path, argv))
        with patched(deploy, ci_state=lambda root: ("master", "master", False),
                     from_ci_refusals=lambda *a: [],
                     ci_sync=lambda root: ("a" * 40, "b" * 40, None)), \
                patched(deploy.os, execv=execv):
            deploy.ci_pull(False, False)
        return calls[0] if calls else None

    with tempfile.TemporaryDirectory() as d:
        clone = os.path.join(d, "bin")
        os.makedirs(clone)
        launcher = os.path.join(clone, "mop")
        open(launcher, "w").close()
        nowhere = os.path.join(d, "nowhere")
        c.expect("#368 a clone: its own bin/mop", fn(clone, py), [launcher])
        c.expect("#368 a package: the interpreter that runs mop",
                 fn(nowhere, py), [py, "-m", "mop.cli"])
        with patched(sys, executable=py):
            with patched(lib, BIN=nowhere):
                c.expect("#368 package: the adapter runs mop via -m mop.cli",
                         adapter_argv(), [py, "-m", "mop.cli", "list", "--all"])
                c.expect("#368 package: ci_pull restarts via -m mop.cli, canonical name",
                         restart_argv(),
                         (py, [py, "-m", "mop.cli", "server", "deploy", "--from-ci"]))
            with patched(lib, BIN=clone):
                c.expect("#368 clone: the adapter runs its bin/mop",
                         adapter_argv(), [launcher, "list", "--all"])
                c.expect("#368 clone: ci_pull restarts its bin/mop, canonical name",
                         restart_argv(),
                         (launcher, [launcher, "server", "deploy", "--from-ci"]))


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
    check_self_argv_368(c)
    check_master_skill_351(c)
    return c.report("packaging")


if __name__ == "__main__":
    sys.exit(main())
