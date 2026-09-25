"""set a person's password: mop server user passwd <login> [--stdin]

The password is asked twice without echo, or read as one line from stdin
with --stdin; the rest of the person's line stays. A login still in
MOP_OPERATORS is refused: mop server user import moves it into the file first.
Silent when all goes well.
"""
from getpass import getpass

from mop.server import identity
from mop.cli import lib
from mop.cli.server.user import _common


def main(argv):
    args, opts = _common.parse(argv, __doc__, flags=("--stdin",))
    if len(args) != 1:
        lib.usage(__doc__)
    got = _common.settings()
    why = _common.refusal(got)
    if why:
        lib.fail(why)
        return 1
    path = identity.operators_path(got)

    def change():
        identity.set_password(path, args[0], _common.password(opts.get("--stdin"), getpass),
                              got.get("MOP_OPERATORS") or "")
    return _common.run(change, path)
