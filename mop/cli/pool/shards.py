"""pool shards: mop shards [--origins] [origin|shard ...]

A shard is a project: one repository, one slice of the pool, one master. The
list is the union of the Nomad roster, the memory in ~/.config/mop/shards and
the arguments (mop/shards.py); naming a new origin here registers it.
"""
import sys

from mop.cli import lib
from mop import shards


def main(argv):
    want_origins = "--origins" in argv
    args = [a for a in argv if a != "--origins"]
    origins, legacy, warnings = shards.collect(args)
    for w in warnings:
        # В stderr: stdout этой команды читают как список, и первый прогон
        # на свежем контроллере отдавал предупреждение git'у как адрес.
        print(w, file=sys.stderr, flush=True)
    shards.remember(origins | legacy)
    for n in (sorted(origins) if want_origins else shards.names(origins, legacy)):
        print(n)


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
