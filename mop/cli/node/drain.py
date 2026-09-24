"""mop node drain <name>: move its puppets off and close it to the scheduler

Graceful: the wrapper takes its session down on TERM, so a puppet leaves
with its clone intact.
"""
from mop.cli import lib
from mop import bus


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "destructive", "args": [
    {"name": "name", "type": "string", "required": True, "help": "node name"}]}


def main(argv):
    if len(argv) != 1:
        lib.usage(__doc__)
    bus.call_cluster("drain", node=argv[0], timeout=60)


main = lib.cluster(main)
