"""mop dev ci list [-n N]: the latest pipelines, newest first

One line each: status, id, short sha, ref, created (UTC), web url.
"""
from mop.common import gitlab
from mop.common.render import table
from mop.cli.dev.ci._common import parser

# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
def main(argv):
    p = parser("list")
    p.add_argument("-n", type=int, default=15)
    a = p.parse_args(argv)
    got = gitlab.pipelines(a.n)
    if not got:
        print("no pipelines")
        return 0
    print("\n".join(table([gitlab.pipeline_line(x) for x in got])))
    return 0
