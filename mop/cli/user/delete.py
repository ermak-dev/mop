"""remove a person: mop user delete <login>

Takes the person's line out of the operators file; the next connection is
refused. Connections already open live on. Silent when all goes well.
"""
from mop.server import identity
from mop.cli import lib
from mop.cli.user import _common


def main(argv):
    args, _ = _common.parse(argv, __doc__)
    if len(args) != 1:
        lib.usage(__doc__)
    got = _common.settings()
    why = _common.refusal(got)
    if why:
        lib.fail(why)
        return 1
    path = identity.operators_path(got)
    return _common.run(lambda: identity.remove_person(path, args[0], got.get("MOP_OPERATORS") or ""),
                       path)
