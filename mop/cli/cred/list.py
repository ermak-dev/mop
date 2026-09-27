"""credentials the server holds: mop cred list

One line per credential: name, profile, kind (login, token, key), owner,
status as of the last probe (active, quota wait, needs login, or unknown
before the first probe), when an exhausted window resets, the worst
window's usage, age, and the puppets holding a lease on it. Secrets
never appear. `mop cred status` probes
the providers now; this prints what the server already knows.
"""
import time

from mop.cli import lib
from mop.common import bus, credreg
from mop.common.render import table


def main(argv):
    if argv:
        lib.usage(__doc__)
    ans = bus.call_cluster("cred_list", project=bus.ADMIN)
    print("\n".join(table(credreg.rows(ans.get("creds") or [], time.time(),
                                       ans.get("holders") or {}))))
    return 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
