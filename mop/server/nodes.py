"""Узлы пула как данные: драйвер, обслуживаемые проекты, состояние, ёмкость.

Одна строка на узел, читают её `mop node` и инструмент `nodes` в MCP (#49).
Модуль возвращает данные и ничего не печатает.
"""
from .. import driver
from . import nomad, spec
from ..common.domain import NodeRow


# Приписки к статусу Nomad в строке state: их собирает row, по ним же
# корзину берёт bucket (#325) -- одно знание, а не два.
DRAINING, CLOSED = ", draining", ", closed"


def bucket(state):
    """Строка state из row -> корзина для дашборда: free -- ready без
    приписки, busy -- draining или closed (узел уводят или закрыли), down --
    прочее. Те же корзины, что страница считала регэкспами (#325)."""
    if state.endswith(DRAINING) or state.endswith(CLOSED):
        return "busy"
    return "free" if state == "ready" else "down"


def row(summary, meta, cap):
    """Сводка узла из ростера + его meta + ёмкость (cluster.nomad_pool) -> строка.

    Состояние: draining старше closed — узел, с которого уводят папетов,
    закрыт для планирования по определению, и сказать только «закрыт»
    значило бы спрятать, что на нём ещё идёт работа. Узел без драйвера в
    meta — host: так ведёт себя узел, до которого deploy не доходил."""
    state = summary["Status"]
    if summary.get("Drain"):
        state += DRAINING
    elif summary.get("SchedulingEligibility") == "ineligible":
        state += CLOSED
    # Опечатка в драйвере одного узла -- строка с отказом, а не отказ всего
    # списка (#175): список отвечает «что где стоит», и чужая опечатка не
    # должна отнимать ответ. Операции над таким узлом отказывают громко.
    try:
        drv, error = driver.of_node(meta, summary["Name"]), None
    except RuntimeError as e:
        # Поле error -- только у такого узла: строка исправного прежняя.
        drv, error = "?", str(e)
    return NodeRow(summary["Name"], driver=drv, error=error,
                # mop_projects — ключ меты УЗЛА, прежнее имя (#85): его
                # объявляет клиент Nomad, и переименование оставило бы старые
                # спеки без узлов, которые их принимают.
                serves=meta.get(spec.META_PROJECTS) or "-", state=state,
                free_mb=cap.get("free_mb"), total_mb=cap.get("total_mb"),
                slots=cap.get("slots"), slots_total=cap.get("slots_total")).to_row()


def nomad_rows(pool, api=None):
    """Все узлы кластера, по имени. Ёмкость есть только у ready-узлов пула,
    у остальных None — прочерк, а не ноль.

    Из Nomad, то есть только на сервере: зовёт это сервис кластера, глагол
    `nodes` (docs/CLUSTER.md). pool — ёмкость узлов пула (cluster.nomad_pool):
    аргументом, а не импортом, потому что сервис кластера сам импортирует этот
    модуль (#152). api -- Nomad (nomad.NomadApi, #275): сервис кластера
    передаёт свой, по умолчанию живой (#375)."""
    api = api or nomad
    cap = {n["name"]: n for n in pool}
    metas = api.nodes_meta()
    return [row(n, metas.get(n["Name"], {}), cap.get(n["Name"], {}))
            for n in sorted(api.get_nodes(), key=lambda n: n["Name"])]

