"""log a claude.ai account in on the server: mop cred login <name> [--setup-token]

Runs the official client (`claude auth login`, or `claude setup-token`
with --setup-token) in a pty with the credential's own home,
~/.config/mop/creds/<name>/, prints the authorize url and waits for the
code on stdin: open the url in any browser, sign in, paste the code the
page shows. Silent about secrets: the session lands in the credential's
home (.claude/.credentials.json), a setup-token in creds/<name>/token.

`auth login` grants the full scope set (user:profile among them, which the
usage endpoint needs); a setup-token is inference-only. The code is
single-use and the exchange takes up to two minutes. A successful login
registers the credential (its cred.json): an `auth login` with the account's
email as the owner, a setup-token without one.
"""
import os
import sys

from mop.cli import lib
from mop.common import fsutil, paths
from mop.server import credlogin, credreg


def registration(mode, status):
    """Итог входа -> аргументы записи кредита (#307). Чистая функция.

    auth login -- вид login, владелец -- email из `claude auth status`;
    setup-token -- вид token без владельца: его область -- user:inference,
    профиля (а с ним и почты) у него нет."""
    if mode == "setup-token":
        return {"kind": "token", "owner": ""}
    return {"kind": "login", "owner": (status or {}).get("email") or ""}


def main(argv):
    setup = "--setup-token" in argv
    args = [a for a in argv if a != "--setup-token"]
    if len(args) != 1 or args[0].startswith("-") or "/" in args[0]:
        lib.usage(__doc__)
    name = args[0]
    home = paths.local(paths.CREDS, name)
    mode = "setup-token" if setup else "login"
    try:
        login = credlogin.Login.start(home, mode)
    except (RuntimeError, OSError) as e:
        lib.fail(f"{name}: {e}")
        return 1
    print(login.url)
    print("scopes: " + " ".join(credlogin.scopes_of(login.url)), file=sys.stderr)
    print("code: ", end="", file=sys.stderr, flush=True)
    code = sys.stdin.readline().strip()
    if not login.submit(code):
        lib.fail(f"{name}: {login.error}")
        return 1
    # Запись кредита (#307): без неё дом виден в `mop cred list`, но цикл
    # сервиса, probe_all, `mop cred status`, --cred и кнопка страницы его
    # пропускают. Переавторизация сохраняет прежнюю запись (register_login).
    if setup:
        path = os.path.join(home, "token")
        fsutil.write_private(path, login.result + "\n")
        credreg.register_login(name, **registration(mode, {}))
        print(f"{name}: token stored in {path}")
        return 0
    status = credlogin.auth_status(home)
    credreg.register_login(name, **registration(mode, status))
    line = credlogin.status_line(status)
    print(f"{name}: {line or 'logged in'}")
    return 0
