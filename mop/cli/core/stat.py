"""token usage across the pool, a bar per day: mop stat [--days N] [--puppets|--users]

Sums claude token counters (input, output, cache write, cache read) over
every puppet on every ready node for the last N days (default 14), by the
node's local date. --puppets breaks the same window down per puppet, the
biggest spender first, instead of drawing the chart; --users does it per
person -- the login of the master whose message started each turn, "-" for
spend nobody's message started, or a node whose agent does not attribute yet. Read off the puppets'
own transcripts, so a puppet whose clone was removed no longer counts. In a
master shell the picture is the project's, not the pool's.
"""
import shutil

from mop.cli import lib
from mop.common import bus, puppets
from mop import usage
from mop.common.render import table

DEFAULT_DAYS = 14
# Разбор транскриптов — не мгновенный: у узла десяток папетов, у каждого
# транскрипты за окно, и обычные 20с шины на «агент молчит» здесь были бы
# ложным диагнозом.
TIMEOUT = 90


def human(n):
    """1234567 -> 1.2M: на графике важен порядок, а не разряды."""
    for unit, div in (("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= div:
            v = n / div
            return f"{v:.1f}{unit}" if v < 10 else f"{v:.0f}{unit}"
    return str(n)


def parse(argv):
    """-> (дней, разбивка: None | "puppets" | "users")"""
    days, split = DEFAULT_DAYS, None
    it = iter(argv)
    for a in it:
        if a in ("--puppets", "--users"):
            # Одна разбивка на окно: две таблицы разом -- два разных вопроса.
            if split and split != a[2:]:
                lib.usage(__doc__)
            split = a[2:]
            continue
        if a == "--days":
            a = next(it, "")
        elif a.startswith("--days="):
            a = a.split("=", 1)[1]
        else:
            lib.usage(__doc__)
        try:
            days = int(a)
        except ValueError:
            lib.usage(__doc__)
        if days < 1:
            lib.usage(__doc__)
    return days, split


def chart(days, per_day):
    """Строка на день: дата, полоса, итог. Полоса — доля от самого жирного
    дня, ширина — под терминал, чтобы график не ломался переносами."""
    axis = usage.days_back(days)
    totals = {d: usage.total(per_day.get(d, {})) for d in axis}
    peak = max(totals.values()) or 1
    width = max(shutil.get_terminal_size((80, 24)).columns - 12 - 8, 10)
    for d in axis:
        n = totals[d]
        bar = "#" * round(width * n / peak)
        print(f"{d}  {bar.ljust(width)} {human(n) if n else '-':>6}")


def breakdown(per_puppet):
    """Таблица по папетам, от самого прожорливого. per_puppet: {(имя, узел):
    {kind: n}}. Ключ с узлом, а не одно имя: переехавший папет оставляет
    транскрипты на старом узле, и обе строки — его расход."""
    rows = [("PUPPET", "NODE", "INPUT", "OUTPUT", "CACHE-W", "CACHE-R", "TOTAL")]
    order = sorted(per_puppet.items(), key=lambda kv: -usage.total(kv[1]))
    for (name, node), t in order:
        rows.append((name, node, human(t["input"]), human(t["output"]),
                     human(t["cache_write"]), human(t["cache_read"]),
                     human(usage.total(t))))
    print("\n".join(table(rows)))


def by_users(per_user):
    """Таблица по людям, от самого прожорливого (#245). per_user: {логин:
    {kind: n}}; «-» -- неприписанный расход."""
    rows = [("USER", "INPUT", "OUTPUT", "CACHE-W", "CACHE-R", "TOTAL")]
    for login, t in sorted(per_user.items(), key=lambda kv: (-usage.total(kv[1]), kv[0])):
        rows.append((login, human(t["input"]), human(t["output"]),
                     human(t["cache_write"]), human(t["cache_read"]),
                     human(usage.total(t))))
    print("\n".join(table(rows)))


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "readonly", "args": [
    {"name": "days", "type": "integer", "flag": "--days", "help": "window in days, 14 by default"},
    {"name": "puppets", "type": "boolean", "flag": "--puppets", "help": "per puppet instead of per day"},
    {"name": "users", "type": "boolean", "flag": "--users", "help": "per user (who started the turns) instead of per day"}]}


def main(argv):
    days, split = parse(argv)
    nodes = sorted(puppets.ready_nodes())
    if not nodes:
        # Пустой пул -- не «никто не ответил» с пустым перечнем (#163).
        raise RuntimeError("no ready nodes in the pool")
    answers = bus.request_many({n: {"verb": "usage", "days": days} for n in nodes},
                               timeout=TIMEOUT)
    per_day, per_puppet, per_user, failed = {}, {}, {}, []
    for n in nodes:
        a = answers.get(n)
        why = bus.failure(a)
        if why:
            failed.append(f"{n}: {why}")
            continue
        for name, rows in a.get("usage", {}).items():
            per_puppet[(name, n)] = usage.sum_days(rows)
            usage.merge(per_day, rows)
        usage.add_users(per_user, usage.by_user(a))
    # Не `puppets`: так звали бы и модуль, из которого строкой выше берут
    # состав пула, и локальный счётчик — питон трактует такую функцию как
    # использующую локальную переменную до присваивания, и падает на первой
    # же строке.
    counted = len(per_puppet)

    if len(failed) == len(nodes):
        # Ни одного ответа — график из прочерков был бы ложью про пустой пул.
        # Отказ -- исключением, как у прочих командлетов (#146, #163).
        raise RuntimeError("no node answered:\n  " + "\n  ".join(failed))
    project = lib.in_project()
    who = f"project {project}" if project else "whole pool"
    print(f"tokens per day, {who}, last {days} days: "
          f"{counted} puppets on {len(nodes) - len(failed)} nodes")
    if split == "puppets":
        breakdown(per_puppet)
    elif split == "users":
        by_users(per_user)
    else:
        chart(days, per_day)
    grand = usage.sum_days(per_day)
    print(f"total {human(usage.total(grand))}: "
          + ", ".join(f"{k.replace('_', ' ')} {human(grand[k])}" for k in usage.KINDS))
    for f in failed:
        print(f"  not counted — {f}")



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
