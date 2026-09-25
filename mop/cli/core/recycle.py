"""recreate a puppet on a clean working copy: mop recycle <name> [--force]

Unsaved changes are wiped by restoring from git, the target directory is
removed entirely (the first build after a recycle is slow). The clone
itself isn't recloned. The puppet's workspace (.mop/bootstrap.yaml) comes
from the working copy this runs in, or from the origin elsewhere.

Another master's puppet (work in its clone, or dispatched minutes ago) is
refused with that master's name; --force acts anyway and says whose it was.
"""
from mop.cli import lib
from mop.cli.core import _common
from mop import puppets


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
    # workspace -- из рабочей копии, откуда зовут (#133): удалённый файл
    # снимается и на сервере.
    p = lib.Progress(name)
    p.step("stopping, resetting the clone, wiping target")
    try:
        r = puppets.recycle(name, workspace_of=_common.workspace_text, force=force)
    finally:
        p.clear()
    if r.get("owner_note"):
        print(f"{name}: {r['owner_note']}")
    return 0



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
