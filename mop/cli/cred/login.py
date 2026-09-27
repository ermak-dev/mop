"""log a claude.ai account in on the server: mop cred login <name> [--setup-token]

Runs the official client (`claude auth login`, or `claude setup-token`
with --setup-token) in a pty with the credential's own home,
~/.config/mop/creds/<name>/, prints the authorize url and waits for the
code on stdin: open the url in any browser, sign in, paste the code the
page shows. Silent about secrets: the session lands in the credential's
home (.claude/.credentials.json), a setup-token in creds/<name>/token.

`auth login` grants the full scope set (user:profile among them, which the
usage endpoint needs); a setup-token is inference-only. The code is
single-use and the exchange takes up to two minutes.
"""
import os
import sys

from mop.cli import lib
from mop.common import fsutil, paths
from mop.server import credlogin


def main(argv):
    setup = "--setup-token" in argv
    args = [a for a in argv if a != "--setup-token"]
    if len(args) != 1 or args[0].startswith("-") or "/" in args[0]:
        lib.usage(__doc__)
    name = args[0]
    home = paths.local("creds", name)
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
    if setup:
        path = os.path.join(home, "token")
        fsutil.write_private(path, login.result + "\n")
        print(f"{name}: token stored in {path}")
        return 0
    line = credlogin.status_line(credlogin.auth_status(home))
    print(f"{name}: {line or 'logged in'}")
    return 0
