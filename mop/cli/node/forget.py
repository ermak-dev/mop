"""mop node forget <name>: drop the node from the roster

Refuses while anything still runs there — a node dropped from under a live
puppet keeps working and the master never hears of it again.
"""
from mop.cli import lib
from mop import bus


def main(argv):
    """Убрать узел из ростера. Отказ — громкий и с причиной: решение
    необратимо, а забытый из-под живого папета узел продолжает работать."""
    if len(argv) != 1:
        lib.usage(__doc__)
    name = argv[0]
    # Предохранитель считает сервис кластера: он же и снимает узел, и решение
    # с проверкой не должны жить на разных машинах — иначе между ними успеет
    # приехать папет (mop/nomad.py, forget_refusal).
    got = bus.ask_cluster("forget", node=name, timeout=30)
    if got.get("error"):
        lib.usage(got["error"])
    print(f"{name} is out of the roster. Take it out of the inventory too, "
          f"or the next deploy will configure it again.")


main = lib.cluster(main)
