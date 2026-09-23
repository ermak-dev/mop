"""restart a stuck puppet: mop restart <name>

The clone and branch survive a restart — the wrapper comes up on the same
directory.
"""
from mop.cli import lib
from mop import nomad


def main(argv):
    if len(argv) != 1:
        lib.usage(__doc__)
    name = argv[0]
    lib.guard(name)
    a = lib.running_alloc(name)
    print(f"restarting {name} on {a['NodeName']}...")
    try:
        nomad.alloc_restart(a["ID"])
        print("puppet is restarting")
    except Exception as e:
        print(f"restart failed: {e}")
        print(f"Alternative: mop delete {name} && mop add <origin>")



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
