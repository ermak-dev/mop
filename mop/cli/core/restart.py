"""restart a stuck puppet: mop restart <name> [--force]

The clone and branch survive a restart — the wrapper comes up on the same
directory.

Another master's puppet (work in its clone, or dispatched minutes ago) is
refused with that master's name; --force acts anyway and says whose it was.
"""
from mop.cli import lib
from mop import bus, puppets


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "destructive", "args": [
    {"name": "name", "type": "string", "required": True, "help": "puppet name, pu-<project>-<n>"},
    {"name": "force", "type": "boolean", "flag": "--force",
     "help": "act on a puppet another master leads; the answer names whom"}]}


def main(argv):
    force = "--force" in argv
    argv = [a for a in argv if a != "--force"]
    if len(argv) != 1:
        lib.usage(__doc__)
    name = argv[0]
    lib.guard(name)
    # Не работает -- отказ с причиной (LookupError) до запроса рестарта.
    puppets.running_alloc(name)
    try:
        got = bus.call_cluster("restart", name=name, owner=bus.login(), force=force)
    except bus.Refused as e:
        raise bus.Refused(f"restart failed: {e}\n"
                          f"Alternative: mop delete {name} && mop add <origin>")
    # Молча на успехе; чью аренду прошёл force -- называем (#40).
    if got.get("owner_note"):
        print(f"{name}: {got['owner_note']}")



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
