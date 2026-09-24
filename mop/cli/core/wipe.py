"""wipe a puppet's working copy: mop wipe <name>

The clone is reset to HEAD (unsaved and untracked changes are gone), the
target directory is removed entirely. The agent refuses if the tmux session
is alive: a bare wipe is for a stopped puppet — the full cycle is mop recycle.
"""
from mop.cli import lib
from mop import bus, puppets


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "destructive", "args": [
    {"name": "name", "type": "string", "required": True, "help": "puppet name, pu-<project>-<n>"}]}


def main(argv):
    if len(argv) != 1:
        lib.usage(__doc__)
    name = argv[0]
    lib.guard(name)
    alloc = bus.call_cluster("alloc", name=name).get("alloc")
    if not alloc:
        raise LookupError(f"{name}: no allocation — node unknown")
    r = puppets.wipe(alloc["NodeName"], name)
    print(f"{name}: clone reset to HEAD, target wiped ({r['target']})")



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
