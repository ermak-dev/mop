"""mop node up <name>: open the node to the scheduler again
"""
from mop.cli import lib
from mop import bus


def main(argv):
    if len(argv) != 1:
        lib.usage(__doc__)
    got = bus.ask_cluster("up", node=argv[0])
    if got.get("error"):
        lib.usage(got["error"])
    print(f"{argv[0]} is open to the scheduler again")


main = lib.cluster(main)
