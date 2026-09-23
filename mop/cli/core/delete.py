"""delete a puppet: mop delete <name>

Where the body is the node itself, the clone stays and is reused if a puppet
of that name comes back. Where the body is a container, it goes with the
puppet: the clone lives inside it, and a body nobody owns is a running
container holding memory and disk.
"""
from mop.cli import lib
from mop import puppets


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "destructive", "args": [
    {"name": "name", "type": "string", "required": True, "help": "puppet name, pu-<project>-<n>"}]}


def main(argv):
    if len(argv) != 1:
        lib.usage(__doc__)
    name = argv[0]
    lib.guard(name)
    r = puppets.delete(name)
    if r["body"] == "destroyed":
        print(f"deleted {name} (body gone from {r['node']})")
    elif r["body"] == "kept":
        print(f"deleted {name} (clone left at {puppets.clone_dir(name)} on {r['node']})")
    else:
        print(f"deleted {name} (no allocation — nothing to clean up)")



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
