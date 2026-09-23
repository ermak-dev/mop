"""mop node up <name>: open the node to the scheduler again
"""
from mop.cli import lib
from mop import nomad


def main(argv):
    if len(argv) != 1:
        lib.usage(__doc__)
    nomad.node_eligibility(argv[0], True)
    print(f"{argv[0]} is open to the scheduler again")


main = lib.cluster(main)
