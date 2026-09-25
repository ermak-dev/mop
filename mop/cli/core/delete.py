"""delete a puppet: mop delete <name> [--force]

Where the body is the node itself, the clone stays and is reused if a puppet
of that name comes back. Where the body is a container, it goes with the
puppet: the clone lives inside it, and a body nobody owns is a running
container holding memory and disk.

Another master's puppet (work in its clone, or dispatched minutes ago) is
refused with that master's name; --force acts anyway and says whose it was.
"""
from mop.cli import lib
from mop.common import puppets


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
    r = puppets.delete(name, force=force)
    if r.get("owner_note"):
        print(f"{name}: {r['owner_note']}")
    if r["body"] == "destroyed":
        print(f"deleted {name} (body gone from {r['node']})")
    elif r["body"] == "kept":
        print(f"deleted {name} (clone left at {puppets.clone_dir(name)} on {r['node']})")
    else:
        print(f"deleted {name} (no allocation — nothing to clean up)")



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
