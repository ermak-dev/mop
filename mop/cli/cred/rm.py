"""remove a credential: mop cred rm <name>

Takes the credential's home on the server down with its secret. Puppets
holding it keep the copy they were given until the next hand-out.
Silent on success.
"""
from mop.cli import lib
from mop.common import bus


def main(argv):
    if len(argv) != 1 or argv[0].startswith("-"):
        lib.usage(__doc__)
    bus.call_cluster("cred_rm", project=bus.ADMIN, name=argv[0])
    return 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
