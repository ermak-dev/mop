"""Узлы пула как данные: драйвер, обслуживаемые проекты, состояние, ёмкость.

Одна строка на узел, читают её `mop node` и инструмент `nodes` в MCP (#49).
Модуль возвращает данные и ничего не печатает.
"""
from . import driver, nomad


def row(summary, meta, cap):
    """Сводка узла из ростера + его meta + ёмкость (cluster.nomad_pool) -> строка.

    Состояние: draining старше closed — узел, с которого уводят папетов,
    закрыт для планирования по определению, и сказать только «закрыт»
    значило бы спрятать, что на нём ещё идёт работа. Узел без драйвера в
    meta — host: так ведёт себя узел, до которого deploy не доходил."""
    state = summary["Status"]
    if summary.get("Drain"):
        state += ", draining"
    elif summary.get("SchedulingEligibility") == "ineligible":
        state += ", closed"
    # Опечатка в драйвере одного узла -- строка с отказом, а не отказ всего
    # списка (#175): список отвечает «что где стоит», и чужая опечатка не
    # должна отнимать ответ. Операции над таким узлом отказывают громко.
    try:
        drv, error = driver.of_node(meta, summary["Name"]), {}
    except RuntimeError as e:
        # Поле error -- только у такого узла: строка исправного прежняя.
        drv, error = "?", {"error": str(e)}
    return {"name": summary["Name"],
            "driver": drv,
            **error,
            # mop_projects — ключ меты УЗЛА, прежнее имя (#85): его объявляет
            # клиент Nomad, и переименование оставило бы старые спеки без
            # узлов, которые их принимают.
            "serves": meta.get("mop_projects") or "-",
            "state": state,
            "free_mb": cap.get("free_mb"),
            "total_mb": cap.get("total_mb"),
            "slots": cap.get("slots")}


def nomad_rows(pool):
    """Все узлы кластера, по имени. Ёмкость есть только у ready-узлов пула,
    у остальных None — прочерк, а не ноль.

    Из Nomad, то есть только на сервере: зовёт это сервис кластера, глагол
    `nodes` (docs/CLUSTER.md). pool — ёмкость узлов пула (cluster.nomad_pool):
    аргументом, а не импортом, потому что сервис кластера сам импортирует этот
    модуль (#152)."""
    cap = {n["name"]: n for n in pool}
    metas = nomad.nodes_meta()
    return [row(n, metas.get(n["Name"], {}), cap.get(n["Name"], {}))
            for n in sorted(nomad.client().nodes.get_nodes(), key=lambda n: n["Name"])]


def rows():
    """То же через шину — для всех, кроме сервера. Глагол оператора: строка
    узла говорит, чьи образы на нём собраны и кто его делит."""
    from . import bus
    return bus.ask_cluster("nodes").get("nodes") or []
