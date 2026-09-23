"""puppet limit of a project: mop project limit [<name> <N|none>]

  mop project limit                 every project's limit
  mop project limit rugent 3        at most 3 puppets for rugent
  mop project limit rugent 0        frozen: no new puppets
  mop project limit rugent none     no limit

The limit is the project's, not a person's: the cluster service sees which
project a request is about, not who sent it. It is checked by the add verb
(mop add), against the project's jobs that are not dead; puppets already
running above a newly lowered limit are left alone.

Kept on the server and set by the operator's verb project_limit (#117); the
service reads it on every add, so no restart is needed.
"""
from mop.cli import lib
from mop import bus, projects


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "destructive", "args": [
    {"name": "name", "type": "string", "help": "project; without it, every project's limit"},
    {"name": "limit", "type": "string", "help": "N, 0 to freeze, none to lift"}]}


def main(argv):
    if not argv:
        ans = bus.ask_cluster("projects", project=bus.ADMIN)
        if ans.get("error"):
            lib.fail(ans["error"])
            return 1
        for n in ans.get("names") or []:
            print(f"{n}\t{(ans.get('limits') or {}).get(n, 'none')}")
        return 0
    if len(argv) != 2 or argv[0].startswith("-"):
        lib.usage(__doc__)
    name = argv[0]
    try:
        value = projects.parse_limit(argv[1])
    except ValueError as e:
        lib.usage(f"{e}\n{__doc__}")
    ans = bus.ask_cluster("project_limit", project=bus.ADMIN, name=name, value=value)
    if ans.get("error"):
        lib.fail(f"{name}: {ans['error']}")
        return 1
    return 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
