"""take a project off the pool: mop project delete <name>

The project leaves the pool together with its puppet limit; its puppets
lose the bus. Refuses while the project still has puppets — they would keep
working with no one able to reach them — delete its puppets first
(mop delete pu-<project>-<n>). The repository is not touched.
"""
from mop.cli import lib
from mop.common import bus


def main(argv):
    if len(argv) != 1 or argv[0].startswith("-"):
        lib.usage(__doc__)
    name = argv[0]
    try:
        bus.call_cluster("project_delete", project=bus.ADMIN, name=name)
    except bus.Refused as e:
        raise bus.Refused(f"{name}: {e}")
    return 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
