#!/usr/bin/env python3
"""Автовход перед запуском сессий без живой шины: python3 tests/autologin.py."""
import io
import os
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
        with patched(login, complete=finish), patched_env(MOP_BUS_PASSWORD="bus-password"):
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


def main():
    c = Checks()
    check_ensure_408(c)
    check_launchers_408(c)
    return c.report("autologin")


if __name__ == "__main__":
    sys.exit(main())
