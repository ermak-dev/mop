"""space on pool nodes: mop server disk [node]

df on the filesystem where puppet clones and target directories live.
This is a node-level verb run only on the server: host space belongs
to the whole pool, not to one project.
"""
from mop.cli import lib
from mop.cli.server import _maintenance
from mop.common import bus, puppets
from mop.common.render import table


def main(argv):
    if len(argv) > 1:
        lib.usage(__doc__)
    nodes = [argv[0]] if argv else sorted(puppets.ready_nodes())
    answers = bus.request_many("disk", nodes)
    rows = [("NODE", "FS", "FREE", "TOTAL")]
    for n in nodes:
        a = answers.get(n)
        why = bus.failure(a)
        if why:
            rows.append((n, why, "", ""))
        else:
            rows.append((n, a["path"], f"{a['free_gb']} GB", f"{a['total_gb']} GB"))
    print("\n".join(table(rows)))



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(_maintenance.only_server(main))
