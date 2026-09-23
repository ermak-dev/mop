"""projects the pool serves: mop project list [--origins]

Reads the registry (~/.config/mop/projects). --origins prints the origins
themselves, which is what `mop deploy` feeds to the manifests; without it,
project names — the same names the bus users are built from.
"""
import sys

from mop.cli import lib
from mop import shards


def main(argv):
    want_origins = "--origins" in argv
    if [a for a in argv if a != "--origins"]:
        lib.usage(__doc__)
    origins, legacy, note = shards.registry()
    if note:
        # В stderr: stdout этой команды читают как список, и предупреждение
        # в нём однажды уехало git'у как адрес (#68).
        print(note, file=sys.stderr, flush=True)
    for n in (sorted(origins) if want_origins else shards.names(origins, legacy)):
        print(n)


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
