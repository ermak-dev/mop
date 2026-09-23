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
from mop import bus, creds, puppets, shards

PLAYBOOK = "deploy/projects.yml"


def main(argv):
    if len(argv) != 1 or argv[0].startswith("-"):
        lib.usage(__doc__)
    name = argv[0]
    origins, legacy, note = shards.registry()
    if note:
        print(note, file=sys.stderr, flush=True)
    lines, dropped = shards.without_project(name, origins | legacy)
    if not dropped:
        lib.usage(f"no project {name} in {shards.FILE}.\n"
                  f"What the pool serves: mop project list")

    alive = [j["ID"] for j in puppets.jobs(shard=bus.ADMIN)
             if puppets.shard_of((j.get("Meta") or {}).get("origin", "")) == name]
    if alive:
        lib.usage(f"{name} still has puppets: {', '.join(sorted(alive))}.\n"
                  f"Delete them first: mop delete {sorted(alive)[0]}")

    shards.write(lines)
    print(f"  {shards.FILE}: {', '.join(dropped)} dropped")

    lib.section(f"ansible: {PLAYBOOK}")
    rc = lib.play(PLAYBOOK, shards.names(*puppets.shard_ids(lines)))
    if rc:
        lib.fail(f"ansible exited {rc}; {name} is out of the registry but may still "
                 f"be on the bus — run mop project delete {name} again")
        return rc

    # Пароль мастера снятого проекта читается как «проект на шине есть»
    # (lib.shard_ready): `mop master` поднялся бы, чтобы не подключиться.
    gone = creds.forget(name, os.path.expanduser("~/.config/mop/secrets"),
                        creds.server_dir())
    lib.ok(f"  {name}: off the bus; {len(gone)} password file(s) removed here")
    print("  other operators still hold theirs — those stop working, "
          "and mop join <server> removes them")
    return 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
