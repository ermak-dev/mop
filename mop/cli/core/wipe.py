"""wipe a puppet's working copy: mop wipe <name> [--force]

The clone is reset to HEAD (unsaved and untracked changes are gone), the
target directory is removed entirely. The agent refuses if the tmux session
is alive: a bare wipe is for a stopped puppet — the full cycle is mop recycle.

Another master's puppet (work in its clone, or dispatched minutes ago) is
refused with that master's name; --force acts anyway and says whose it was.
"""
from mop.cli import lib
from mop.common import bus, puppets
from mop.common.domain import Gone


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "destructive", "args": [
    {"name": "name", "type": "string", "required": True, "help": "puppet name, pu-<project>-<n>"},
    {"name": "force", "type": "boolean", "flag": "--force",
     "help": "act on a puppet another master leads; the answer names whom"}]}


def main(argv):
    name, force = lib.named(argv, __doc__)
    alloc = bus.call_cluster("alloc", name=name).get("alloc")
    if not alloc:
        raise LookupError(f"{name}: no allocation — node unknown")
    r = puppets.wipe(alloc["NodeName"], name, force=force)
    lib.note(name, r)
    print(f"{name}: clone reset to HEAD, target wiped ({Gone.from_dict(r).target})")



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
