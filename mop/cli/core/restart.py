"""restart a stuck puppet: mop restart <name> [--force]

The clone and branch survive a restart — the wrapper comes up on the same
directory.

Another master's puppet (work in its clone, or dispatched minutes ago) is
refused with that master's name; --force acts anyway and says whose it was.
"""
from mop.cli import lib
from mop.common import bus, ops, puppets


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "destructive", "node_verbs": [], "cluster_verbs": ['alloc', 'restart'], "args": [
    {"name": "name", "type": "string", "required": True, "help": "puppet name, pu-<project>-<n>"},
    {"name": "force", "type": "boolean", "flag": "--force",
     "help": "act on a puppet another master leads; the answer names whom"}]}


def main(argv):
    name, force = lib.named(argv, __doc__)
    # Не работает -- отказ с причиной (LookupError) до запроса рестарта.
    puppets.running_alloc(name)
    v = ops.restart(bus.call_cluster, name, force=force, login=bus.login())
    if v.get("outcome") == ops.REFUSED:
        raise bus.Refused(f"restart failed: {v['error']}\n"
                          f"Alternative: mop delete {name} && mop add <origin>")
    # Молча на успехе; чью аренду прошёл force -- называем (#40).
    lib.note(name, v)


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
