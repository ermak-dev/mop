"""take a project off the bus: mop project delete <name>

Asks the cluster service on the server (operator's verb project_delete):
the project leaves the server's registry, its NATS user and bus
password go, and so does its puppet limit.

Refuses while the project still has puppets: a master whose puppets are
alive would lose the bus under them, and the puppets would keep working
with no one able to hear them. Delete them first (mop delete <name>).

The repository is not touched: this is about the pool, not about the code.
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
