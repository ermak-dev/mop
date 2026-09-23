"""mop node drain <name>: move its puppets off and close it to the scheduler

Graceful: the wrapper takes its session down on TERM, so a puppet leaves
with its clone intact.
"""
from mop.cli import lib
from mop import nomad


def main(argv):
    if len(argv) != 1:
        lib.usage(__doc__)
    name = argv[0]
    print(f"draining {name}: puppets leave, no new ones arrive...")
    nomad.node_drain(name)
    print(f"{name} is closed to the scheduler. Watch them land: mop list")


main = lib.cluster(main)
