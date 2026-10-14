"""mop session terminal <name> <action> [tid] [options]: private terminal RPC

Actions: open, read, input, interrupt, resize, close.
  open       --mode ro|rw --cols N --rows N (default ro, 100 by 30)
  read       --cursor N --wait N (default 0, at most 15 seconds)
  input      --seq N --data BASE64 (at most 4096 decoded bytes)
  interrupt  --seq N (Escape to the existing CLI, not job stop)
  resize     --cols N --rows N (writer only)
  close      detaches this client, never kills the puppet

Open returns a private tid. Every subsequent action needs it. Input sequences
start at 1 and advance consecutively; an unknown receipt must not be retried
with a new sequence. Terminal output is base64 bytes for an emulator, not text
with terminal escapes removed. Operator-only node verbs enforce read-only and ownership.
"""
import base64
import binascii

from mop.cli import lib
from mop.common import bus
from mop.cli.session._common import context, parser, show

MCP = {"annotations": "destructive", "args": [
    {"name": "name", "type": "string", "required": True, "help": "puppet name"},
    {"name": "action", "type": "string", "required": True, "help": "open, read, input, interrupt, resize or close"},
    {"name": "tid", "type": "string", "help": "private attachment id except for open"},
    {"name": "mode", "type": "string", "flag": "--mode", "help": "ro by default; rw explicitly takes control"},
    {"name": "seq", "type": "integer", "flag": "--seq", "help": "consecutive input sequence, starting at 1"},
    {"name": "data", "type": "string", "flag": "--data", "help": "bounded base64 keyboard bytes"},
    {"name": "cols", "type": "integer", "flag": "--cols", "help": "20 to 500 columns"},
    {"name": "rows", "type": "integer", "flag": "--rows", "help": "5 to 200 rows"},
    {"name": "cursor", "type": "integer", "flag": "--cursor", "help": "last terminal byte cursor"},
    {"name": "wait", "type": "integer", "flag": "--wait", "help": "long-poll wait, at most 15 seconds"},
]}


def main(argv):
    p = parser(__doc__)
    p.add_argument("name")
    p.add_argument("action", choices=("open", "read", "input", "interrupt", "resize", "close"))
    p.add_argument("tid", nargs="?")
    p.add_argument("--mode", choices=("ro", "rw"), default="ro")
    p.add_argument("--cols", type=int, default=100)
    p.add_argument("--rows", type=int, default=30)
    p.add_argument("--cursor", type=int, default=0)
    p.add_argument("--wait", type=int, default=0)
    p.add_argument("--seq", type=int)
    p.add_argument("--data")
    a = p.parse_args(argv)
    if (not 20 <= a.cols <= 500 or not 5 <= a.rows <= 200
            or not 0 <= a.wait <= 15 or a.cursor < 0):
        lib.usage("invalid terminal dimensions, cursor or wait")
    fields = {}
    if a.action == "open":
        if a.tid:
            lib.usage("open does not take a terminal id")
        fields.update(mode=a.mode, cols=a.cols, rows=a.rows)
    else:
        if not a.tid or len(a.tid) > 128 or not a.tid.isascii() \
                or not all(ch.isalnum() or ch in "-_" for ch in a.tid):
            lib.usage("a valid terminal id is required")
        fields["tid"] = a.tid
        if a.action == "read":
            fields.update(cursor=a.cursor, wait=a.wait)
        elif a.action in ("input", "interrupt"):
            if a.seq is None or a.seq < 1:
                lib.usage("input needs a positive sequence")
            data = "Gw==" if a.action == "interrupt" else a.data
            try:
                raw = base64.b64decode(data, validate=True) if isinstance(data, str) and len(data) <= 5464 else None
            except (ValueError, binascii.Error):
                raw = None
            if raw is None or not 0 < len(raw) <= 4096:
                lib.usage("input needs bounded base64 bytes")
            fields.update(seq=a.seq, data=data)
        elif a.action == "resize":
            fields.update(cols=a.cols, rows=a.rows)
    target, actor = context(a.name)
    action = "write" if a.action in ("input", "interrupt") else a.action
    try:
        result = bus.request(target["node"], f"term_{action}", project=bus.ADMIN,
                             name=a.name, owner=actor, **fields)
    except bus.Refused as e:
        result = {"error": str(e), "outcome": "refused"}
    except bus.BusError as e:
        if action != "write":
            raise
        result = {"error": str(e), "outcome": "unknown", "seq": a.seq, "written": None}
    except Exception:
        if action != "write":
            raise
        result = {"error": "terminal input could not be confirmed", "outcome": "unknown",
                  "seq": a.seq, "written": None}
    return show(result)


main = lib.cluster(main)
