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
from mop import nodes
from mop.render import table


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "readonly"}


def main(argv):
    if argv:
        lib.usage(__doc__)
    """Узлы пула как таблица. То же, что видно в подвале `mop list`, плюс то,
    чего там нет: драйвер, чьи проекты узел умеет и состояние планирования."""
    got = nodes.rows()
    rows = [("NODE", "DRIVER", "SERVES", "STATE", "FREE", "TOTAL", "SLOTS")]
    for r in got:
        rows.append((
            r["name"], r["driver"], r["serves"], r["state"],
            f"{r['free_mb'] // 1024} GB" if r["free_mb"] is not None else "-",
            f"{r['total_mb'] // 1024} GB" if r["total_mb"] is not None else "-",
            str(r["slots"]) if r["slots"] is not None else "-",
        ))
    print("\n".join(table(rows)))
    # Узел с неизвестным драйвером -- в таблице с «?», причина строкой ниже.
    for r in got:
        if r.get("error"):
            print(r["error"])


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
