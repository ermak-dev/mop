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
    name = argv[0]
    print(f"draining {name}: puppets leave, no new ones arrive...")
    got = bus.ask_cluster("drain", node=name, timeout=60)
    if got.get("error"):
        lib.usage(got["error"])
    print(f"{name} is closed to the scheduler. Watch them land: mop list")


main = lib.cluster(main)
