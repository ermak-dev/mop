"""credentials the server holds: mop cred list

One line per credential: name, profile, kind (login, token, key), owner,
status as of the last probe (active, quota wait, needs login, or unknown
before the first probe), when an exhausted window resets, the worst
window's usage, age, and the puppets holding a lease on it. Secrets
never appear. `mop cred status` probes
the providers now; this prints what the server already knows.
"""
from mop.cli import lib
from mop.cli.cred import _common
from mop.common import bus


def main(argv):
    if argv:
        lib.usage(__doc__)
    _common.print_creds(bus.call_cluster("cred_list", project=bus.ADMIN))
    return 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
