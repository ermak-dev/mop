"""restart a stuck puppet: mop restart <name>

The clone and branch survive a restart — the wrapper comes up on the same
directory.
"""
from mop.cli import lib
from mop import bus


def main(argv):
    if len(argv) != 1:
        lib.usage(__doc__)
    name = argv[0]
    lib.guard(name)
    a = lib.running_alloc(name)
    print(f"restarting {name} on {a['NodeName']}...")
    got = bus.ask_cluster("restart", name=name)
    if got.get("error"):
        print(f"restart failed: {got['error']}")
        print(f"Alternative: mop delete {name} && mop add <origin>")
        return 1
    print("puppet is restarting")



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
