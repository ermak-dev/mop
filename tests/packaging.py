#!/usr/bin/env python3
"""Упаковка клиента без пула и без сети: python3 tests/packaging.py

Клиент ставится пакетом из git одной командой (#351):
`uv tool install git+ssh://<origin>/ermak/mop.git`. Метаданные, зависимости,
self_argv и скилл проверяются отдельно. #642 собирает wheel и ставит его с
объявленными client dependencies в свой venv из wheelhouse окружения
(PIP_FIND_LINKS, без сети); installed CLI/MCP читают собственный TLS fixture.
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
    # #580: пакет везёт каталог ролей целиком (skills/*/*), не один файл
    # скилла мастера: у каждой роли soul.yaml и промпт (docs/SOUL.md).
    c.check("#351 the role catalogue ships as package data",
            "skills/*/*" in data.get("mop", []))
    # #627/#644: контроллер везёт собранный дашборд (index и ассеты), сервер
    # не имеет ни node, ни рабочей копии. Живая отдача -- tests/packaged_dashboard.py.
    controller = data.get("mop.server.controller", [])
    c.check("#627 the packaged dashboard ships in the wheel",
            "dist/index.html" in controller and "dist/assets/*" in controller)


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
# #662: mcp_command ушёл вместе с локальным мастером; старт MCP-сервера
# задаёт спека роли, и клиентский конфиг ему не нужен.
def check_master_mcp_351(c):
    import tempfile
    from mop.cli import lib
    with tempfile.TemporaryDirectory() as d:
        clone = os.path.join(d, "bin")
        os.makedirs(clone)
        launcher = os.path.join(clone, "mop")
        open(launcher, "w").close()
        c.expect("#351 a clone launches itself",
                 lib.self_argv(bin_dir=clone), [launcher])
    # #662: mcp_config ушёл вместе с локальным мастером; сервер MCP
    # поднимает спека роли и mcp-командлет, а не клиентский конфиг.


# ── #368: MCP-инструменты и перезапуск --from-ci зовут себя и из пакета ──
# HYPOTHESIS: адаптер MCP зовёт командлеты как <cli.BIN>/mop, а ci_pull
# перезапускает себя как <lib.BIN>/mop deploy --from-ci. У пакета bin/ нет:
# каждый инструмент-командлет отдаёт модели [Errno 2], а перезапуск падает;
# вдобавок ci_pull зовёт прежнее имя из LEGACY, которое уйдёт.
# SOLUTION: lib.self_argv(bin_dir, python) по правилу #351 -- bin/mop клона,
# если он есть, иначе [python, -m, mop.cli]; ci_pull сохраняет каноническое
# `server deploy --from-ci`. #642: MCP теперь вызывает dispatcher в процессе;
# его пригодность из installed wheel проверяется ниже, не argv subprocess.
# STATUS: FIXED — see #368
def check_self_argv_368(c):
    import tempfile
    from _lib import patched
    from mop.cli import lib
    fn = getattr(lib, "self_argv", None)
    if not c.check("#368 lib.self_argv exists", fn is not None):
        return
    from mop.cli.server import deploy
    py = "/venv/bin/python"

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
                 fn(nowhere, py), [py, "-P", "-m", "mop.cli"])
        with patched(sys, executable=py):
            with patched(lib, BIN=nowhere):
                c.expect("#368 package: ci_pull restarts via -m mop.cli, canonical name",
                         restart_argv(),
                         (py, [py, "-P", "-m", "mop.cli", "server", "deploy", "--from-ci"]))
            with patched(lib, BIN=clone):
                c.expect("#368 clone: ci_pull restarts its bin/mop, canonical name",
                         restart_argv(),
                         (launcher, [launcher, "server", "deploy", "--from-ci"]))


# HYPOTHESIS (#642): старый adapter_argv падал на удалённом mcp.subprocess;
# путь до bin/mop сам по себе не доказывает, что wheel пригоден для клиента.
# SOLUTION: чистая установка wheel с объявленными client dependencies, без
# server extra, вне checkout. CLI и MCP вызывают тот же bound commandlet и
# читают один canonical pool DTO через собственный verified TLS listener.
# RESULT: исходный strict RED -- AttributeError mcp.subprocess, не env failure.
# STATUS: FIXED — see #642
def check_installed_client_642(c):
    import http.server
    import json
    from pathlib import Path
    import shutil
    import subprocess
    import tempfile
    from _control_fixture import own_tls, pool_page, TOKEN

    class Pool(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.server.calls.append((self.path, self.headers.get('Authorization')))
            authorized = self.path == '/api/v1/puppets?page_size=100' and \
                self.headers.get('Authorization') == 'Token ' + TOKEN
            raw = json.dumps(pool_page() if authorized else {}).encode()
            self.send_response(200 if authorized else 403)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / 'source'
        source.mkdir()
        shutil.copytree(Path(ROOT) / 'mop', source / 'mop',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('setup.py', 'pyproject.toml'):
            shutil.copyfile(Path(ROOT) / name, source / name)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('MOP_', 'PYTHON'))}
        wheels = root / 'wheels'
        build = subprocess.run([sys.executable, '-I', '-m', 'pip', 'wheel', '--no-deps',
            '--no-index', '--disable-pip-version-check', '--wheel-dir', str(wheels), str(source)],
            cwd=root, env=env, capture_output=True, text=True, timeout=90)
        if not c.expect('#642 build the actual wheel from this tree', build.returncode, 0):
            c.fail('#642 wheel build diagnostic', build.stdout + build.stderr)
            return
        home = root / 'venv'
        create = subprocess.run([sys.executable, '-I', '-m', 'venv', str(home)],
            cwd=root, env=env, capture_output=True, text=True, timeout=60)
        if not c.expect('#642 create an isolated installed-client environment', create.returncode, 0):
            c.fail('#642 venv diagnostic', create.stdout + create.stderr)
            return
        wheel, = wheels.glob('mop-*.whl')
        python = home / 'bin' / 'python'
        install = subprocess.run([str(python), '-I', '-m', 'pip', 'install', '--no-index',
            '--disable-pip-version-check', str(wheel)], cwd=root, env=env,
            capture_output=True, text=True, timeout=90)
        if not c.expect('#642 install declared client dependencies from the wheelhouse', install.returncode, 0):
            c.fail('#642 client installation diagnostic', install.stdout + install.stderr)
            return
        # Не копия commandlet: production registry и production tool_call.
        script = '''
import importlib.util, json, sys
from importlib.metadata import distribution
from pathlib import Path
from mop import cli
from mop.cli import _control, _mcp
import mcp.types
assert importlib.util.find_spec('django') is None
assert importlib.util.find_spec('rest_framework') is None
assert importlib.util.find_spec('psycopg') is None
assert Path(cli.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
entry, = distribution('mop').entry_points.select(name='mop', group='console_scripts')
assert entry.value == 'mop.cli:console'
assert (Path(cli.__file__).parents[1] / 'skills/master/SKILL.md').is_file()
words, path, declaration = _mcp.commandlets()['list']
assert words == ['list']
assert Path(path).resolve().is_relative_to(Path(sys.prefix).resolve())
text, status = _control.tool_call(words, declaration, {}, with_status=True)
assert status == 0, text
assert not any(name == 'django' or name == 'nats' or name.startswith(('django.', 'mop.server.', 'nats.'))
               for name in sys.modules)
# Legacy self_argv нужен ci_pull, но не входит в canonical MCP import path.
from mop.cli import lib
assert lib.self_argv() == [sys.executable, '-P', '-m', 'mop.cli']
print(text)
'''
        with own_tls(Pool) as (server, origin, ca, _):
            server.calls = []
            env.update(MOP_API_ORIGIN=origin, MOP_API_TOKEN=TOKEN, MOP_API_CA=str(ca))
            listed = subprocess.run([str(home / 'bin' / 'mop'), 'list'], cwd=root, env=env,
                capture_output=True, text=True, timeout=20)
            adapted = subprocess.run([str(python), '-I', '-c', script], cwd=root, env=env,
                capture_output=True, text=True, timeout=20)
            for name, result in (('console', listed), ('MCP dispatcher', adapted)):
                if c.expect('#642 installed ' + name + ' is usable', result.returncode, 0):
                    c.expect('#642 installed ' + name + ' preserves canonical pool facts',
                             json.loads(result.stdout), pool_page())
                c.expect('#642 installed ' + name + ' has no stderr', result.stderr, '')
                c.check('#642 installed ' + name + ' never prints credentials',
                        TOKEN not in result.stdout + result.stderr)
            c.expect('#642 console and MCP share the canonical authenticated query', server.calls,
                     [('/api/v1/puppets?page_size=100', 'Token ' + TOKEN)] * 2)
    c.expect('#642 server dependencies remain a separate declared extra',
             load_setup().requirements('server'), list(deps.SERVER))
    c.check('#642 no server requirement leaks into client requirements',
            not set(deps.PIP) & set(deps.SERVER))


def check_master_skill_351(c):
    pkg = os.path.join(ROOT, "mop")
    skill = os.path.join(pkg, "skills", "master")
    c.expect("#351 the skill lives inside the package",
             os.path.realpath(skill), skill)
    c.check("#351 the skill file is there",
            os.path.isfile(os.path.join(skill, "SKILL.md")))
    old = os.path.join(ROOT, "skills", "master")
    c.check("#351 the old path is a transition symlink",
            os.path.islink(old) and os.path.realpath(old) == os.path.join(pkg, "skills", "master"))


def main():
    c = Checks()
    check_dependencies_351(c)
    check_console_351(c)
    check_master_mcp_351(c)
    check_self_argv_368(c)
    check_installed_client_642(c)
    check_master_skill_351(c)
    return c.report("packaging")


if __name__ == "__main__":
    sys.exit(main())
