"""disk: what the disk watchdog would sweep on each node, and who is under pressure

Сторож диска -- глагол агента sweep (#358): pu-sweep исполняется на самом
узле, он знает локальное (tmux-сессии, клоны, тела), а doctor добирается до
него через шину. diagnose метёт всухую, --fix -- по-настоящему: `mop doctor`
без --fix не сносит ничего, как и в остальных группах. Узел, где мести
нечего и места хватает, проблемой не считается.
"""
from mop.common import bus, puppets

# pu-sweep меряет du каждое дерево, а под давлением гоняет cargo-sweep по
# живым target-ам: минуты, а не секунды. Меньший таймаут читал бы живого
# агента молчащим.
SWEEP_TIMEOUT = 600
NAME = "disk"


def human(kb):
    """Килобайты -> «1.5 GB» -- та же запись, что у самого pu-sweep."""
    if kb >= 1024 * 1024:
        return f"{kb / (1024 * 1024):.1f} GB"
    if kb >= 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{kb} KB"


def orphans(n):
    """Число брошенных тел -> «2 orphaned bodies» -- так же, как считает
    `mop driver sweep`."""
    return f"{n} orphaned bod{'y' if n == 1 else 'ies'}"


def issue(node, answer):
    """Ответ глагола sweep (всухую) -> проблема узла | None. Чистая функция.
    Молчание агента -- проблема с причиной, а не пропуск (#163): узел,
    которого не спросили, не подметён."""
    got = bus.verdict(answer)
    if got and got[0] == bus.UNREACHED:
        return {"name": NAME, "node": node, "alloc": None, "action": None,
                "diagnosis": f"agent silent, not swept: {got[1] or 'no answer'}"}
    if got:
        return {"name": NAME, "node": node, "alloc": None, "action": None,
                "diagnosis": f"sweep FAILED: {got[1]}"}
    freed, free, floor = answer.get("freed_kb"), answer.get("free_gb"), answer.get("min_gb")
    # Узел с телами-контейнерами места не считает (ярус 0 уходит без итоговой
    # строки), он считает тела: без этого числа брошенные тела не становились
    # проблемой и --fix их не мёл (#363).
    bodies = answer.get("bodies")
    pressure = free is not None and floor is not None and free < floor
    parts = []
    if pressure:
        parts.append(f"disk pressure: {free} GB free < {floor} GB")
    if freed:
        parts.append(f"{human(freed)} to sweep")
        if not pressure and free is not None:
            parts.append(f"{free} GB free")
    if bodies:
        parts.append(f"{orphans(bodies)} to sweep")
    # Под давлением ярус 3 подрежет живые target-ы и при пустом «to sweep»:
    # всухую он не считает, сколько снимет.
    action = "sweep" if (freed or pressure or bodies) else None
    text = "; ".join(p for p in (", ".join(parts), *(answer.get("warnings") or ())) if p)
    if not text:
        return None
    return {"name": NAME, "node": node, "alloc": None, "diagnosis": text, "action": action}


def issues(nodes, answers):
    """{узел: ответ | BusError} -> [проблема], узлы по алфавиту."""
    return [i for i in (issue(n, answers.get(n)) for n in sorted(nodes)) if i]


def diagnose():
    nodes = list(puppets.ready_nodes())
    try:
        answers = bus.request_many("sweep", nodes, timeout=SWEEP_TIMEOUT, dry=True)
    except bus.BusError as e:
        answers = {n: e for n in nodes}
    return issues(nodes, answers)


def prepare(issues):
    return None, []


def treat(issue):
    node = issue["node"]
    try:
        answer = bus.request(node, "sweep", timeout=SWEEP_TIMEOUT)
    except bus.BusError as e:
        answer = e
    got = bus.verdict(answer)
    if got:
        return f"{node}: sweep FAILED: {got[1] or 'no answer'}"
    freed, free = answer.get("freed_kb"), answer.get("free_gb")
    if answer.get("bodies") is not None:
        return f"{node}: destroyed {orphans(answer['bodies'])}"
    if freed is None:
        return f"{node}: swept, no totals"
    return f"{node}: freed {human(freed)}, {free} GB free"
