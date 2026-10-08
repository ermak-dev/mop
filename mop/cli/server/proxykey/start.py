"""start a rotation: mop server proxykey start --nodes n1,n2 --exclusive

Checks the controller boundary, resolves the controller source and the
cluster service copy, freezes the recipient set (--nodes, or
--all-registered for the full known node set, offline included, or
--no-recipients), and records the private checkpoint. No network effect:
the remote steps belong to resume. --exclusive affirms the caller is the
only writer (panel included); without it the command refuses.
"""
from mop.cli import lib
from mop.cli.server.proxykey import _common
from mop.server import proxykey


def main(argv):
    args, opts = _parse(argv)
    if "--exclusive" not in opts:
        lib.fail("start requires --exclusive: affirm no other writer "
                 "(panel, deploy) is active")
        return 1
    why = _common.refusal()
    if why:
        lib.fail(why)
        return 1
    nodes = _common.recipients(opts)
    if isinstance(nodes, str):
        lib.fail(nodes)
        return 1
    src, cp = _resolve()
    try:
        out = proxykey.begin(src, cp, nodes, exclusive=True,
                             effects=_common.effects())
    except RuntimeError as e:
        lib.fail(str(e))
        return 1
    return _common.show(out, "--json" in opts)


def _parse(argv):
    """argv -> (позиционные, опции). Повторяемый --nodes суммируется."""
    args, opts = [], {}
    it = iter(argv)
    for a in it:
        if a == "--nodes":
            got = next(it, None)
            if not got:
                lib.usage(__doc__)
            opts["--nodes"] = (opts.get("--nodes", "")
                               + ("," if opts.get("--nodes") else "")
                               + got)
        elif a in ("--exclusive", "--all-registered", "--no-recipients",
                   "--json"):
            opts[a] = True
        elif a.startswith("-"):
            lib.usage(__doc__)
        else:
            args.append(a)
    return args, opts


def _resolve():
    from mop.server import proxyapi
    return proxyapi.resolve_paths()
