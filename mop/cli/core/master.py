"""launch the master role as a new pooled puppet: mop master [git-origin]

The same launcher as `mop <role>` for the role master, and the command
says so: a new puppet of the project is registered with the role, waits
for its bootstrap and the role's session, and the terminal attaches to it
over ssh. Detaching leaves the puppet running; reconnect with
`mop attach <name>`, which never creates one. Only origin belongs to a
role launch: the role owns its prompt, settings and permissions, and any
flag is refused before anything is allocated. A failure after
registration names the created puppet and does not create another.

Any other role of the catalogue launches the same way: `mop <role>`. A
role whose name is a mop command or a reserved bus name is refused before
anything is allocated. The local claude launch is gone: a master is a
pooled puppet; for claude outside the pool there is `mop code`.
"""
from mop.cli import lib
from mop.cli.core import _launch

ANNOUNCE = "mop master: launching role master as a new pooled puppet"


def main(argv):
    # Явный алиас мастер-роли (#586): тот же общий лаунчер, что `mop <роль>`,
    # и строка о том, что именно запускается.
    print(ANNOUNCE, flush=True)
    return _launch.launch("master", argv,
                          hint="claude outside the pool: mop code")


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
