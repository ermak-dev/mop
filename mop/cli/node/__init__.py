"""pool nodes: mop node [drain|up|forget <name>]

  mop node                 what the pool stands on: driver, projects, capacity
  mop node drain <name>    move its puppets off and close it to the scheduler
  mop node up <name>       open it to the scheduler again
  mop node forget <name>   drop it from the roster

Draining is graceful: the wrapper takes its session down on TERM, so a
puppet leaves with its clone intact. `forget` refuses while anything still
runs there — a node dropped from under a live puppet keeps working and the
master never hears of it again — and while the node is in the inventory,
which the next deploy would configure it from again.
"""
from mop.cli import lib
from mop.common import puppets
from mop.common.render import ratio, table


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "readonly"}


def main(argv):
    if argv:
        lib.usage(__doc__)
    """Узлы пула как таблица. То же, что видно в подвале `mop list`, плюс то,
    чего там нет: драйвер, чьи проекты узел умеет и состояние планирования."""
    got = puppets.nodes()
    rows = [("NODE", "DRIVER", "SERVES", "STATE", "FREE", "TOTAL", "SLOTS")]
    for n in got:
        rows.append((
            n.name, n.driver, n.serves, n.state,
            f"{n.free_mb // 1024} GB" if n.free_mb is not None else "-",
            f"{n.total_mb // 1024} GB" if n.total_mb is not None else "-",
            ratio(n.slots, n.slots_total),
        ))
    print("\n".join(table(rows)))
    # Узел с неизвестным драйвером -- в таблице с «?», причина строкой ниже.
    for n in got:
        if n.error:
            print(n.error)


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
