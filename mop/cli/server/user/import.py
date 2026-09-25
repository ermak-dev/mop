"""transitional: move MOP_OPERATORS into the operators file: mop server user import

Every MOP_OPERATORS entry becomes a line of the file with its current
password (the one mop server deploy made in secrets/nats-op-<login>.pass, now as a
hash), its role and projects; name and email are empty (mop server user delete and
add again, or edit the line). All or nothing: a login already in the file,
or one without a password, refuses the whole move. The operator.json on
people's machines keeps working, no new mop join.

Afterwards remove MOP_OPERATORS from .env and run mop server deploy: until then
each moved login is defined twice and refused. Goes away together with
MOP_OPERATORS.
"""
from mop.server import identity
from mop.cli import lib
from mop.cli.server.user import _common


def main(argv):
    if argv:
        lib.usage(__doc__)
    got = _common.settings()
    why = _common.refusal(got)
    if why:
        lib.fail(why)
        return 1
    path = identity.operators_path(got)
    try:
        moved = identity.import_setting(path, got.get("MOP_OPERATORS") or "")
    except ValueError as e:
        lib.fail(str(e))
        return 1
    why = identity.refresh_copy(path)
    if why:
        print(why)
    # Одна строка, и не молча: без следующего шага эти логины не входят.
    print(f"moved {', '.join(moved)} into {path}: remove MOP_OPERATORS from .env "
          f"and run mop server deploy -- until then they are defined twice and refused")
    return 0
