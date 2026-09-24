"""mop node up <name>: open the node to the scheduler again
"""
from mop.cli import lib
from mop import bus


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "destructive", "args": [
    {"name": "name", "type": "string", "required": True, "help": "node name"}]}


def main(argv):
    if len(argv) != 1:
        lib.usage(__doc__)
    bus.call_cluster("up", node=argv[0])
    print(f"{argv[0]} is open to the scheduler again")


main = lib.cluster(main)
