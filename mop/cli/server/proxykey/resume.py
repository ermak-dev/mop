"""resume the rotation: mop server proxykey resume --exclusive

Drives the recorded operation from its checkpoints to the first
blocker: the rotation library decides every step; the adapter
supplies the effects. The frozen recipient set stands as recorded;
membership is never re-selected. Pending or refusal is rc 1 with a
safe reason; a quiet fast success prints one line. --exclusive
affirms the caller is still the only writer.
"""
from mop.cli import lib
from mop.cli.server.proxykey import _common
from mop.server import proxykey


def main(argv):
    if argv and argv[0] != "--exclusive" and argv[0] != "--json":
        lib.usage(__doc__)
        return 1
    as_json = "--json" in argv
    if "--exclusive" not in argv:
        lib.fail("resume requires --exclusive: affirm no other writer "
                 "(panel, deploy) is active")
        return 1
    why = _common.refusal()
    if why:
        lib.fail(why)
        return 1
    try:
        out = proxykey.resume(exclusive=True, effects=_common.effects())
    except RuntimeError as e:
        lib.fail(str(e))
        return 1
    return _common.show(out, as_json)
