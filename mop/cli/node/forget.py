"""mop node forget <name>: drop the node from the roster

Refuses while anything still runs there — a node dropped from under a live
puppet keeps working and the master never hears of it again.

Refuses as well while the node is in the controller's inventory: the next
mop server deploy would configure it again. Take it out of the inventory and run
mop server deploy first.
"""
from mop.cli import lib
from mop.common import bus


def main(argv):
    """Убрать узел из ростера. Отказ — громкий и с причиной: решение
    необратимо, а забытый из-под живого папета узел продолжает работать."""
    if len(argv) != 1:
        lib.usage(__doc__)
    name = argv[0]
    # Предохранитель считает сервис кластера: он же и снимает узел, и решение
    # с проверкой не должны жить на разных машинах — иначе между ними успеет
    # приехать папет (mop/server/nomad.py, forget_refusal).
    bus.call_cluster("forget", node=name, timeout=30)


main = lib.cluster(main)
