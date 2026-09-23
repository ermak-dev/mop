"""mop node forget <name>: drop the node from the roster

Refuses while anything still runs there — a node dropped from under a live
puppet keeps working and the master never hears of it again.
"""
from mop.cli import lib
from mop import nomad


def main(argv):
    """Убрать узел из ростера. Отказ — громкий и с причиной: решение
    необратимо, а забытый из-под живого папета узел продолжает работать."""
    if len(argv) != 1:
        lib.usage(__doc__)
    name = argv[0]
    node = nomad.node_summary(name)
    if node is None:
        lib.usage(f"no node {name} in the cluster")
    why = nomad.forget_refusal(node, nomad.node_allocs(name))
    if why:
        lib.usage(why)
    nomad.node_forget(name)
    print(f"{name} is out of the roster. Take it out of the inventory too, "
          f"or the next deploy will configure it again.")


main = lib.cluster(main)
