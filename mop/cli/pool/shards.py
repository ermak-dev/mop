"""pool shards: mop shards [--origins] — renamed to mop project list

Kept while the epic that renames shard to project is in flight (#78): the
word is in operators' fingers, in the master skill and in the docs. It
prints the same list and says where the command went.

The arguments are gone with it: naming an origin here used to register a
project, and that is `mop project add` now (#79).
"""
import sys

from mop.cli import lib
from mop import shards


def main(argv):
    if [a for a in argv if a != "--origins"]:
        lib.usage(f"mop shards no longer registers anything.\n"
                  f"Register a project: mop project add <git-origin>\n"
                  f"What the pool serves: mop project list")
    print("mop shards is now mop project list", file=sys.stderr, flush=True)
    origins, legacy, note = shards.registry()
    if note:
        print(note, file=sys.stderr, flush=True)
    for n in (sorted(origins) if "--origins" in argv
              else shards.names(origins, legacy)):
        print(n)


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
