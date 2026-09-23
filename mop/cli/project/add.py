"""register a project and put it on the bus: mop project add <git-origin>

Writes the origin into the registry (~/.config/mop/projects) and plays
deploy/projects.yml — the NATS user puppet-<project> on the server, the
puppet credentials on every node. Operators reach the project by their role
in MOP_OPERATORS (admin and user:* at once, a user with a list once it is
named there). Nothing else: this is
what the appearance of a project actually changes.

Idempotent, and that is also the repair: run it again and the credentials
are rendered again from the registry.

A project's own .mop manifest (sandbox and bootstrap) is NOT played here —
it belongs to the node and body layers, and those are `mop deploy`. The
command says so when the project has one.

A node that is down when this runs does not get the credentials; it picks
them up at the next `mop deploy`, and the command says which case it hit.
"""
import sys

from mop.cli import lib
from mop import manifest, puppets, projects

PLAYBOOK = "deploy/projects.yml"


def main(argv):
    if len(argv) != 1 or argv[0].startswith("-"):
        lib.usage(__doc__)
    origin = argv[0]
    origins, legacy, note = projects.registry()
    if note:
        print(note, file=sys.stderr, flush=True)
    # Отказ на голое имя — здесь, до сетевых вызовов: origin'ом проект
    # заводится, им же режется манифест и им же клонируется папет.
    lines, added = projects.with_origin(origin, origins | legacy)
    name = puppets.project_of(origin)

    # Недоступный origin валит команду громко: заведённый проект обязан
    # существовать, иначе пользователь на шине есть, а клонировать нечего.
    got = manifest.fetch(origin)
    needs_deploy = [k for k in ("sandbox_vars", "sandbox_tasks",
                                "bootstrap_vars", "bootstrap_tasks") if got[k]]

    projects.write(lines)
    print(f"  {projects.FILE}: {name} "
          + ("registered" if added else "was already registered"))

    lib.section(f"ansible: {PLAYBOOK}")
    rc = lib.play(PLAYBOOK, projects.names(*puppets.project_ids(lines)))
    if rc and rc != lib.UNREACHABLE:
        lib.fail(f"ansible exited {rc}; {name} is in the registry but not on the "
                 f"bus — run mop project add {origin} again")
        return rc
    if rc == lib.UNREACHABLE:
        # Выключенный узел — не отказ команды: проект доехал до всех, кто
        # ответил. Но доехал НЕ ДО ВСЕХ, и молчать об этом нельзя: папет
        # проекта, вставший на такой узел, не найдёт кредов и прочитается как
        # «агент молчит» на пустом месте.
        lib.fail(f"some machines did not answer; {name} reached every machine that "
                 f"did. A node that was down gets the credentials at the next "
                 f"mop deploy; if the server was the one missing, nothing reached "
                 f"the bus — run mop project add {origin} again")

    # Везти оператору нечего (#106): пароля на проект больше нет, человек
    # ходит своим именем, и новый проект ему открывает роль.
    lib.ok(f"  {name}: on the bus")
    print("  operators reach it by their role in MOP_OPERATORS: admin and "
          "user:* already do, a user with a list needs it named and a "
          "mop deploy")
    if needs_deploy:
        print(f"  {name}/.mop: {', '.join(needs_deploy)} — played by the node and "
              f"body layers, run mop deploy to play them")
    return rc


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
