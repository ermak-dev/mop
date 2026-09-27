"""probe the providers now: mop cred status [name]

Asks each provider (or the one named) about its credential — is it alive,
how full are its windows, when do they reset — stores the answer on the
server and prints the same table as `mop cred list`. A claude login whose
access token has run out is refreshed first (the client does it itself,
one tiny request).
"""
from mop.cli import lib
from mop.cli.cred import _common
from mop.common import bus


def main(argv):
    if len(argv) > 1 or any(a.startswith("-") for a in argv):
        lib.usage(__doc__)
    fields = {"name": argv[0]} if argv else {}
    _common.print_creds(bus.call_cluster("cred_status", project=bus.ADMIN, timeout=180, **fields))
    return 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
