"""recreate a puppet on a clean working copy: mop recycle <name>

Unsaved changes are wiped by restoring from git, the target directory is
removed entirely (the first build after a recycle is slow). The clone
itself isn't recloned. The puppet's workspace (.mop/bootstrap.yaml) comes
from the working copy this runs in, or from the origin elsewhere.
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
    # workspace -- из рабочей копии, откуда зовут (#133): удалённый файл
    # снимается и на сервере.
    p = lib.Progress(name)
    p.step("stopping, resetting the clone, wiping target")
    try:
        puppets.recycle(name, workspace_of=lib.workspace_text)
    finally:
        p.clear()
    return 0



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
