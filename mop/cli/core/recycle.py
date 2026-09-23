"""recreate a puppet on a clean working copy: mop recycle <name>

Unsaved changes are wiped by restoring from git, the target directory is
removed entirely (the first build after a recycle is slow). The clone
itself isn't recloned.
"""
from mop.cli import lib
from mop import puppets


def main(argv):
    if len(argv) != 1:
        lib.usage(__doc__)
    name = argv[0]
    lib.guard(name)
    print(f"stopping {name}, resetting clone to HEAD, wiping target...")
    r = puppets.recycle(name)
    print(f"recreated {name} on {r['node']}")



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
