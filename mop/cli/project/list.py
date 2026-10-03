"""projects the pool serves: mop project list [--origins]

--origins prints the origins themselves; without it, project names.
"""
from mop.cli import lib
from mop.common import bus


def main(argv):
    want_origins = "--origins" in argv
    if [a for a in argv if a != "--origins"]:
        lib.usage(__doc__)
    ans = bus.call_cluster("projects", project=bus.ADMIN)
    for n in ans.get("origins" if want_origins else "names") or []:
        print(n)
    return 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
