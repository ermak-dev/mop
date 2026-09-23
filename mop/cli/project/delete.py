"""take a project off the bus: mop project delete <name>

Drops it from the registry (~/.config/mop/projects), plays
deploy/projects.yml — the NATS users go out of the server config, the
puppet credentials off the nodes — and removes its passwords here.

Refuses while the project still has puppets: a master whose puppets are
alive would lose the bus under them, and the puppets would keep working
with no one able to hear them. Delete them first (mop delete <name>).

The repository is not touched: this is about the pool, not about the code.
"""
import os
import sys

from mop.cli import lib
from mop import bus, creds, puppets, projects

PLAYBOOK = "deploy/projects.yml"


def main(argv):
    if len(argv) != 1 or argv[0].startswith("-"):
        lib.usage(__doc__)
    name = argv[0]
    origins, legacy, note = projects.registry()
    if note:
        print(note, file=sys.stderr, flush=True)
    lines, dropped = projects.without_project(name, origins | legacy)
    if not dropped:
        lib.usage(f"no project {name} in {projects.FILE}.\n"
                  f"What the pool serves: mop project list")

    alive = [j["ID"] for j in puppets.jobs(project=bus.ADMIN)
             if puppets.project_of((j.get("Meta") or {}).get("origin", "")) == name]
    if alive:
        lib.usage(f"{name} still has puppets: {', '.join(sorted(alive))}.\n"
                  f"Delete them first: mop delete {sorted(alive)[0]}")

    projects.write(lines)
    print(f"  {projects.FILE}: {', '.join(dropped)} dropped")

    lib.section(f"ansible: {PLAYBOOK}")
    rc = lib.play(PLAYBOOK, projects.names(*puppets.project_ids(lines)))
    if rc and rc != lib.UNREACHABLE:
        lib.fail(f"ansible exited {rc}; {name} is out of the registry but still on "
                 f"the bus — run mop project delete {name} again")
        return rc
    if rc == lib.UNREACHABLE:
        # Тут недоступная машина опаснее, чем при заводе: на ней остаётся
        # лежать кред снятого проекта. Пользователя на шине уже нет, так что
        # представиться им нельзя, но секрет лежит — и уйдёт следующим deploy.
        lib.fail(f"some machines did not answer; a node that was down still holds "
                 f"{name}'s credentials until the next mop deploy. If the server "
                 f"was the one missing, its bus users are still there — run "
                 f"mop project delete {name} again")

    # Пароль мастера снятого проекта читается как «проект на шине есть»
    # (lib.project_ready): `mop master` поднялся бы, чтобы не подключиться.
    gone = creds.forget(name, os.path.expanduser("~/.config/mop/secrets"),
                        creds.server_dir())
    lib.ok(f"  {name}: off the bus; {len(gone)} password file(s) removed here")
    print("  operators need nothing: their rights end with the project's "
          "subjects")
    return rc


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
