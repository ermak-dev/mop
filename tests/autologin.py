#!/usr/bin/env python3
"""Автовход перед запуском сессий без живой шины: python3 tests/autologin.py."""
import io
import os
import subprocess
import sys
import tempfile

import hermetic  # noqa: F401,E402
from _lib import Checks, patched, patched_env  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.cli.pool import login  # noqa: E402
from mop.cli.core import code, master  # noqa: E402
from mop.common import config, context, creds  # noqa: E402


# HYPOTHESIS: master отказывает до тела командлета, code доходит до exec
# без входа, а при одном старом operator.json клиент не дополнен прокси.
# SOLUTION: общая локальная проверка обоих файлов; полный вход не трогать,
# частичный дополнить через complete, не угадывать при двух серверах.
# RESULT: master/code не запускают сессию без входа, полный вход не трогают,
# частичный дополняют, неоднозначность и no-TTY отказывают до exec.
# STATUS: FIXED — see #408
def check_ensure_408(c):
    ensure = getattr(login, "ensure", None)
    if not c.check("#408 shared login preflight exists", ensure is not None):
        return
    root = os.path.join(tempfile.mkdtemp(prefix="mop-auto-login-"), "servers")
    dest = os.path.join(root, "srv.test")
    calls, bindings = [], []
    def fake_git(*argv):
        if argv[:2] == ("rev-parse", "--show-toplevel"):
            return "project"
        if argv and argv[0] == "config":
            bindings.append(argv)
        return None
    def finish(host, who, directory, port):
        calls.append((host, who, directory, port))
        creds.write_operator(directory, who, "bus-password")
        creds.write_client(directory, port, f"https://{host}/llm", "proxy-key")
    with patched(creds, ROOT=root), patched(login, complete=finish, git=fake_git), \
            patched_env(MOP_BUS_PASSWORD="bus-password"), \
            patched(context, clone_binding=lambda: {}), \
            context.use(context.resolve({"server": "srv.test", "user": "alice"}, {}, {})):
        ensure()
        c.expect("#408 fresh login completes both credentials", calls,
                 [("srv.test", "alice", dest, "443")])
        c.expect("#408 auto-login binds its project clone", bindings,
                 [("config", "--local", "mop.server", "srv.test"),
                  ("config", "--local", "mop.user", "alice")])
        calls.clear()
        ensure()
        c.expect("#408 complete login does not prompt or reconnect", calls, [])
        os.remove(os.path.join(dest, creds.CLIENT_FILE))
        ensure()
        c.expect("#408 operator-only login fetches proxy config", calls,
                 [("srv.test", "alice", dest, "443")])
    other = os.path.join(root, "other.test")
    creds.write_operator(other, "bob", "bus-password")
    os.remove(os.path.join(dest, creds.CLIENT_FILE))
    with patched(creds, ROOT=root), context.use(context.resolve({}, {}, {})):
        try:
            ensure()
            c.fail("#408 two partial logins require --server")
        except RuntimeError as e:
            c.check("#408 ambiguity is explicit", "server" in str(e).lower(), str(e))
        os.remove(os.path.join(other, creds.OPERATOR_FILE))
        # #530: ensure() с успешной заглушкой complete доходит до _bind.
        # Без подмены git настоящий `git config --local` писал привязку в
        # .git/config репозитория cwd (раннер `mop dev test` -- это корень
        # рабочей копии): после проверок #529 настоящий клон получил
        # mop.server=srv.test и mop.user=alice. Здесь интерес -- выбор
        # единственного прежнего operator, не привязка: git -- как у всех
        # сценариев ensure выше.
        with patched(login, complete=finish, git=fake_git), \
                patched_env(MOP_BUS_PASSWORD="bus-password"):
            ensure()
        c.expect("#408 sole previous operator is selected", calls[-1][0], "srv.test")

    empty = os.path.join(tempfile.mkdtemp(prefix="mop-empty-login-"), "servers")
    with patched(creds, ROOT=empty), patched(sys, stdin=io.StringIO("")), \
            context.use(context.resolve({}, {}, {})):
        try:
            ensure()
            c.fail("#408 no TTY without server must refuse")
        except RuntimeError as e:
            c.check("#408 no-TTY error names login", "mop login" in str(e), str(e))


def check_launchers_408(c):
    calls = []
    def missing():
        calls.append("login")
        raise RuntimeError("mop login required")
    with patched(login, ensure=missing), patched(config, require=lambda *a: calls.append("require")), \
            patched(master.lib, origin=lambda *a: "git@h:g/mop.git",
                    project_ready=lambda *a: True), \
            patched(master, link_skill=lambda: None, mcp_config=lambda: "{}"), \
            patched(master._common, session_env=lambda: {}), \
            patched(master.os, execvpe=lambda *a: calls.append("master exec")):
        try:
            master.main([])
        except RuntimeError:
            pass
        c.expect("#408 master logs in before cluster precheck and exec", calls, ["login"])
        calls.clear()
        try:
            code.main([])
        except RuntimeError:
            pass
        c.expect("#408 code logs in before exec, without pool MCP", calls, ["login"])


# HYPOTHESIS (#427): ensure() запрашивает порт при первом входе на обычный
# TLS-сервер, хотя порт уже известен из окружения или равен 443.
# SOLUTION: один беззвучный выбор порта без сетевого побочного эффекта.
# RESULT: до правки оба ожидания не сбылись; после неё 12 проверок зелёные.
# STATUS: FIXED — see #427
def check_silent_port_427(c):
    root = os.path.join(tempfile.mkdtemp(prefix="mop-auto-427-"), "servers")
    calls, prompts = [], []
    class Tty(io.StringIO):
        def isatty(self):
            return True
    with patched(creds, ROOT=root), patched(login, complete=lambda *a: calls.append(a),
                 _bind=lambda *a: None, ask=lambda p, **kw: prompts.append(p) or "unexpected"), \
            patched(sys, stdin=Tty()), context.use(context.resolve({"server": "srv.test",
                                                                     "user": "alice"}, {}, {})), \
            patched_env(MOP_BUS_PASSWORD="fake-password", MOP_HTTPS_PORT="8443"):
        login.ensure()
        c.expect("#427 preflight uses configured port", calls[-1][3] if calls else None, "8443")
        c.expect("#427 preflight does not ask for port", prompts, [])


# HYPOTHESIS (#530): второй сценарий check_ensure_408 зовёт ensure() с
# заглушкой complete, но БЕЗ подмены login.git. Раннер `mop dev test`
# запускает файл проверок с cwd в корне репозитория, и успешный ensure
# доходит до _bind: настоящий `git config --local mop.server ...` пишет
# привязку в .git/config того репозитория, где лежит cwd, -- после
# проверок #529 в настоящем клоне появились mop.server=srv.test и
# mop.user=alice, и models/list ушёл на тестовый сервер. Первый сценарий
# подменяет git и чист, no-TTY падает до complete -- дыра одна.
# SOLUTION: сценарий ensure исполняется ТОЛЬКО в дочернем процессе с cwd
# во ВРЕМЕННОМ репозитории -- main() не зовёт check_ensure_408 в процессе
# прогона, и при возврате дефекта записи уйдут во временный репозиторий и
# поднимут guard, а не в .git/config настоящего клона. Ребёнок гоняет
# check_ensure_408 целиком, со всеми её проверками, и проваливается кодом
# выхода; родитель требует ребёнка зелёным и -- отдельной пробой git --
# что локальный конфиг временного репозитория не получил ни одной
# mop.*-записи. Проба различает исходы git: 1 -- записей нет, 0 --
# записи есть (возврат дефекта), прочее -- ошибка git с причиной, а не
# молчаливое «пусто». Репозиторий -- на TemporaryDirectory: после
# прогона каталога нет. Локальный конфиг, не --show-origin: враждебный
# прогон hermetic кладёт mop.server и в ~/.gitconfig (#209), и
# глобальные записи -- не эта дыра.
# RESULT: при снятой изоляции сценария (красная проба guard) дочерний
# ensure() оставляет во временном репозитории mop.server=srv.test и
# mop.user=alice, guard краснеет, конфиг настоящего клона неизменен;
# после изоляции ребёнок зелёный и конфиг временного репозитория чист.
# STATUS: FIXED — see #530
def check_ensure_hermetic_530(c):
    tests = os.path.dirname(os.path.realpath(__file__))
    with tempfile.TemporaryDirectory(prefix="mop-auto-530-repo-") as repo:
        r = subprocess.run(["git", "init", "-q", repo], capture_output=True, text=True)
        if not c.check("#530 scenario repository init", r.returncode == 0, r.stderr):
            return
        code = (
            "import sys\n"
            f"sys.path.insert(0, {tests!r})\n"
            "import hermetic  # noqa: F401 -- изоляция настроек (#209)\n"
            "import autologin\n"
            "from _lib import Checks\n"
            "c = Checks()\n"
            "autologin.check_ensure_408(c)\n"
            "sys.exit(1 if c.failed else 0)\n"
        )
        run = subprocess.run([sys.executable, "-c", code], cwd=repo,
                             env={**os.environ, "PYTHONPATH": tests},
                             capture_output=True, text=True)
        c.check("#530 check_ensure_408 stays green in its own process",
                run.returncode == 0, (run.stdout + run.stderr).strip()[-500:])
        leak = subprocess.run(["git", "config", "--local", "--get-regexp", "^mop\\."],
                              cwd=repo, capture_output=True, text=True)
        if leak.returncode == 0:
            c.fail("#530 check_ensure_408 wrote mop.* into the cwd repository's config",
                   leak.stdout.strip())
        elif leak.returncode != 1:
            c.fail("#530 git config probe in the scenario repository failed",
                   f"exit {leak.returncode}: {(leak.stdout + leak.stderr).strip()}")
        else:
            c.check("#530 check_ensure_408 leaves the cwd repository's config clean",
                    not leak.stdout.strip(), leak.stdout.strip())


def main():
    c = Checks()
    # #530: сценарий ensure -- только в дочернем процессе с временным
    # репозиторием: при возврате дефекта записи поднимают guard там, а не
    # в .git/config репозитория cwd прогона.
    check_ensure_hermetic_530(c)
    check_silent_port_427(c)
    check_launchers_408(c)
    return c.report("autologin")


if __name__ == "__main__":
    sys.exit(main())
