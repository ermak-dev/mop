"""Узлы пула как данные: драйвер, обслуживаемые шарды, состояние, ёмкость.

Одна строка на узел, читают её `mop node` и инструмент `nodes` в MCP (#49).
Модуль возвращает данные и ничего не печатает.
"""
from . import driver, nomad, puppets


def row(summary, meta, cap):
    """Сводка узла из ростера + его meta + ёмкость (puppets.pool) -> строка.

    Состояние: draining старше closed — узел, с которого уводят папетов,
    закрыт для планирования по определению, и сказать только «закрыт»
    значило бы спрятать, что на нём ещё идёт работа. Узел без драйвера в
    meta — host: так ведёт себя узел, до которого deploy не доходил."""
    state = summary["Status"]
    if summary.get("Drain"):
        state += ", draining"
    elif summary.get("SchedulingEligibility") == "ineligible":
        state += ", closed"
    return {"name": summary["Name"],
            "driver": meta.get("mop_driver") or driver.DEFAULT,
            "serves": meta.get("mop_shards") or "-",
            "state": state,
            "free_mb": cap.get("free_mb"),
            "total_mb": cap.get("total_mb"),
            "slots": cap.get("slots")}


def rows():
    """Все узлы кластера, по имени. Ёмкость есть только у ready-узлов пула,
    у остальных None — прочерк, а не ноль."""
    cap = {n["name"]: n for n in puppets.pool()}
    metas = nomad.nodes_meta()
    return [row(n, metas.get(n["Name"], {}), cap.get(n["Name"], {}))
            for n in sorted(nomad.client().nodes.get_nodes(), key=lambda n: n["Name"])]
