"""mop session history <name> [--cursor cursor]: read private conversation records

Returns bounded JSON with epoch, session_id, cursor, records, reset, has_more
and partial. Keep the returned cursor for the next read; a reset replaces the
old live conversation. Incomplete JSON lines remain pending. This is not a
token stream. No arbitrary transcript path is accepted.
"""
from mop.cli import lib
from mop.common import bus
from mop.cli.session._common import context, parser, show

MCP = {"annotations": "readonly", "args": [
    {"name": "name", "type": "string", "required": True, "help": "puppet name"},
    {"name": "cursor", "type": "string", "flag": "--cursor", "help": "last private history cursor"},
]}


def main(argv):
    p = parser(__doc__)
    p.add_argument("name")
    p.add_argument("--cursor")
    a = p.parse_args(argv)
    if a.cursor is not None and len(a.cursor) > 4096:
        lib.usage("history cursor exceeds the size limit")
    target, _actor = context(a.name)
    return show(bus.request(target["node"], "history", project=target["project"],
                            name=a.name, **({"cursor": a.cursor} if a.cursor is not None else {})))


main = lib.cluster(main)
