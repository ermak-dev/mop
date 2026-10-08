"""mop server cluster export <DIR>: write the registry as a legacy pair for a rollback

On the server, as the pool user. Reads the live snapshot
(~/.config/mop/projects.json) once, validates it, and lays the pair
(projects, limits.json) out in a NEW directory DIR with a completion
marker export.json; nothing in the live registry, the legacy pair or
the bus is touched, and the command stops no writers.

The rollback itself is an operator's procedure: stop every writer, take
a final export, verify the sha256 in export.json against the source,
then switch. The command does not perform the switch and does not
repair bare names, collisions or orphan limits -- the validator refuses
them, and how to resolve each is a separate decision.
The export carries origin credentials as its private data: the
directory is 0700, the files are 0600, and summaries, markers and
refusals name paths, counts and digests -- never a URL or a token.
An existing destination (file, directory, symlink, or a concurrent
claim) is a refusal: the command never overwrites, and a failed attempt
removes only what that attempt itself created.
"""
import sys

from mop.cli import lib
from mop.common import projects


def main(argv):
    if len(argv) != 1 or argv[0].startswith("-"):
        lib.usage(__doc__)
    try:
        out = projects.export_pair(argv[0])
    except RuntimeError as e:
        sys.exit(f"{e}")
    # Тишина на успехе -- не совсем молчание: одна машиночитаемая строка
    # для оператора отката (счёт, источник, digest, каталог). Креды
    # origin'ов сюда не попадают -- только числа и пути.
    print(f"exported {out['projects']} project(s) to {out['dest']} "
          f"(source {out['source']}, sha256 {out['sha256'][:12]})")
    return 0
