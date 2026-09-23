"""puppet limit of a project: mop project limit [<name> <N|none>]

  mop project limit                 every project's limit
  mop project limit rugent 3        at most 3 puppets for rugent
  mop project limit rugent 0        frozen: no new puppets
  mop project limit rugent none     no limit

The limit is the project's, not a person's: the cluster service sees which
project a request is about, not who sent it. It is checked by the add verb
(mop add), against the project's jobs that are not dead; puppets already
running above a newly lowered limit are left alone.

Kept on the controller (~/.config/mop/limits.json) and carried to the server
by deploy/projects.yml, the same narrow run as mop project add. The cluster
service reads the file on every add, so no restart is needed.
"""
import sys

from mop.cli import lib
from mop import puppets, projects

PLAYBOOK = "deploy/projects.yml"


def main(argv):
    origins, legacy, note = projects.registry()
    if note:
        print(note, file=sys.stderr, flush=True)
    names = projects.names(origins, legacy)
    limits = projects.read_limits()
    if not argv:
        for n in names:
            print(f"{n}\t{limits.get(n, 'none')}")
        return 0
    if len(argv) != 2 or argv[0].startswith("-"):
        lib.usage(__doc__)
    name = argv[0]
    try:
        value = projects.parse_limit(argv[1])
    except ValueError as e:
        lib.usage(f"{e}\n{__doc__}")
    if name not in names:
        lib.usage(f"no project {name} in {projects.FILE}.\n"
                  f"What the pool serves: mop project list")
    projects.write_limits(projects.with_limit(limits, name, value))
    print(f"  {projects.LIMITS}: {name} "
          + ("has no limit" if value is None else f"limited to {value}"))

    lib.section(f"ansible: {PLAYBOOK}")
    rc = lib.play(PLAYBOOK, projects.names(*puppets.project_ids(origins | legacy)))
    if rc and rc != lib.UNREACHABLE:
        lib.fail(f"ansible exited {rc}; the limit is written here but not on the "
                 f"server — run mop project limit {name} {argv[1]} again")
        return rc
    lib.ok(f"  {name}: limit on the server")
    return 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
