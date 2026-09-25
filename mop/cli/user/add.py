"""add a person: mop user add <login> --role admin|user [--projects a,b|*] [--name "..."] [--email ...] [--stdin]

Refuses a login already in the operators file or in MOP_OPERATORS. The role
and projects follow MOP_OPERATORS: admin is the whole pool and takes no
projects; user takes a list, or * for every project. The password is asked
twice without echo, or read as one line from stdin with --stdin. Name and
email sign the person's puppet commits. Silent when all goes well.
"""
from getpass import getpass

from mop import identity
from mop.cli import lib
from mop.cli.user import _common


def main(argv):
    args, opts = _common.parse(argv, __doc__, values=("--role", "--projects", "--name", "--email"),
                               flags=("--stdin",))
    if len(args) != 1:
        lib.usage(__doc__)
    got = _common.settings()
    why = _common.refusal(got)
    if why:
        lib.fail(why)
        return 1
    path = identity.operators_path(got)

    def change():
        who = identity.person(args[0], opts.get("--role") or "", opts.get("--projects") or "",
                              opts.get("--name") or "", opts.get("--email") or "")
        identity.add_person(path, who, _common.password(opts.get("--stdin"), getpass),
                            got.get("MOP_OPERATORS") or "")
    return _common.run(change, path)
