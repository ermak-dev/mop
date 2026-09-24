"""mop ci log <job> [--lines N] [--full]: a job's log

The last 60 lines by default, terminal colors stripped.
"""
from mop import gitlab
from mop.cli import lib
from mop.cli.ci._common import parser

LINES = 60

MCP = {"annotations": "readonly", "args": [
    {"name": "job", "type": "string", "required": True, "help": "job id"},
    {"name": "lines", "type": "integer", "flag": "--lines",
     "help": "how many last lines, 60 by default"},
    {"name": "full", "type": "boolean", "flag": "--full", "help": "the whole log"}]}


def main(argv):
    p = parser("log")
    p.add_argument("job", type=int)
    p.add_argument("--lines", type=int, default=LINES)
    p.add_argument("--full", action="store_true")
    a = p.parse_args(argv)
    text = gitlab.tail(lib.plain(gitlab.trace(a.job)), None if a.full else a.lines)
    print(text or "(empty log)")
    return 0
