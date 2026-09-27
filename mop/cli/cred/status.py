"""probe the providers now: mop cred status [name]

Asks each provider (or the one named) about its credential — is it alive,
how full are its windows, when do they reset — stores the answer on the
server and prints the same table as `mop cred list`. A claude login whose
access token has run out is refreshed first (the client does it itself,
one tiny request).
"""
import time

from mop.cli import lib
from mop.common import bus, credreg
from mop.common.render import table


def main(argv):
    if len(argv) > 1 or any(a.startswith("-") for a in argv):
        lib.usage(__doc__)
    fields = {"name": argv[0]} if argv else {}
    ans = bus.call_cluster("cred_status", project=bus.ADMIN, timeout=180, **fields)
    print("\n".join(table(credreg.rows(ans.get("creds") or [], time.time(),
                                       ans.get("holders") or {}))))
    return 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
