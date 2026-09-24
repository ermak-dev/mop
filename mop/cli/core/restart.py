"""restart a stuck puppet: mop restart <name>

The clone and branch survive a restart — the wrapper comes up on the same
directory.
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
    # Не работает -- отказ с причиной (LookupError) до запроса рестарта.
    puppets.running_alloc(name)
    try:
        bus.call_cluster("restart", name=name)
    except bus.Refused as e:
        raise bus.Refused(f"restart failed: {e}\n"
                          f"Alternative: mop delete {name} && mop add <origin>")



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
