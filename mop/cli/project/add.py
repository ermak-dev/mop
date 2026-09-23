"""register a project and put it on the bus: mop project add <git-origin>

Writes the origin into the registry (~/.config/mop/projects) and plays
deploy/projects.yml — the NATS users master-<project> and puppet-<project>
on the server, the puppet credentials on every node. Nothing else: this is
what the appearance of a project actually changes.

Idempotent, and that is also the repair: run it again and the credentials
are rendered again from the registry.

A project's own .mop manifest (sandbox and bootstrap) is NOT played here —
it belongs to the node and body layers, and those are `mop deploy`. The
command says so when the project has one.
"""
import os
import sys

from mop.cli import lib
from mop import creds, manifest, puppets, shards

PLAYBOOK = "deploy/projects.yml"


def main(argv):
    if len(argv) != 1 or argv[0].startswith("-"):
        lib.usage(__doc__)
    origin = argv[0]
    origins, legacy, note = shards.registry()
    if note:
        print(note, file=sys.stderr, flush=True)
    # Отказ на голое имя — здесь, до сетевых вызовов: origin'ом проект
    # заводится, им же режется манифест и им же клонируется папет.
    lines, added = shards.with_origin(origin, origins | legacy)
    name = puppets.shard_of(origin)

    # Недоступный origin валит команду громко: заведённый проект обязан
    # существовать, иначе пользователь на шине есть, а клонировать нечего.
    got = manifest.fetch(origin)
    needs_deploy = [k for k in ("sandbox_vars", "sandbox_tasks",
                                "bootstrap_vars", "bootstrap_tasks") if got[k]]

    shards.write(lines)
    print(f"  {shards.FILE}: {name} "
          + ("registered" if added else "was already registered"))

    lib.section(f"ansible: {PLAYBOOK}")
    rc = lib.play(PLAYBOOK, shards.names(*puppets.shard_ids(lines)))
    if rc:
        lib.fail(f"ansible exited {rc}; {name} is in the registry but may not be "
                 f"on the bus — run mop project add {origin} again")
        return rc

    # Контроллер — тоже машина оператора: пароль мастера нового проекта
    # обязан оказаться в его каталоге сервера, иначе `mop master` здесь же
    # скажет, что проекта на шине нет.
    dest = creds.server_dir()
    secrets = os.path.expanduser("~/.config/mop/secrets")
    if os.path.isdir(secrets):
        creds.collect(secrets, dest)
        lib.ok(f"  {name}: on the bus; credentials in {dest}")
    else:
        lib.ok(f"  {name}: on the bus")
    print("  other operators pick it up with: mop join <server>")
    if needs_deploy:
        print(f"  {name}/.mop: {', '.join(needs_deploy)} — played by the node and "
              f"body layers, run mop deploy to play them")
    return 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
