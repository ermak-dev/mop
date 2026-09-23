"""register a project and put it on the bus: mop project add [git-origin]

Without origin, the origin of the current working copy is used.

Asks the cluster service on the server (operator's verb project_add, #117):
the server writes the origin into its registry, gives the project a bus
password, adds the NATS user puppet-<project> and checks that the bus lets
it in; if it does not, the server rolls the change back. No ansible and no
controller: any operator's machine with the admin role will do. Operators
reach the project by their role in MOP_OPERATORS.

Idempotent: a project already registered is only checked again.

A project's own .mop manifest (sandbox and bootstrap) is NOT played here —
it belongs to the node and body layers, and those are `mop deploy` on the
server. The command says so when the project has one.
"""
from mop.cli import lib
from mop import bus, manifest, puppets


def main(argv):
    if len(argv) > 1 or any(a.startswith("-") for a in argv):
        lib.usage(__doc__)
    origin = lib.origin(argv[0] if argv else None, __doc__)
    name = puppets.project_of(origin)
    # Недоступный origin валит команду громко и до сервера: заведённый проект
    # обязан существовать, иначе пользователь на шине есть, а клонировать
    # нечего. Читает его машина оператора -- у неё и есть доступ к форжу.
    got = manifest.fetch(origin)
    needs_deploy = [k for k in ("sandbox_vars", "sandbox_tasks",
                                "bootstrap_vars", "bootstrap_tasks") if got[k]]

    ans = bus.ask_cluster("project_add", project=bus.ADMIN, origin=origin)
    if ans.get("error"):
        lib.fail(f"{name}: {ans['error']}")
        return 1
    lib.ok(f"  {name}: " + ("registered and on the bus" if ans.get("added")
                            else "was already registered, on the bus"))
    print("  operators reach it by their role in MOP_OPERATORS: admin and "
          "user:* already do, a user with a list needs it named and a "
          "mop deploy")
    if needs_deploy:
        print(f"  {name}/.mop: {', '.join(needs_deploy)} — played by the node and "
              f"body layers, run mop deploy on the server to play them")
    return 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
