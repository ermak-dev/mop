"""Дашборд пула: снимок для страницы и сборщик, который его держит (#66).

Ещё один фронтенд над теми же данными, что `mop list`, `mop node`,
`mop doctor` и `mop stat`: библиотека отдаёт строки, здесь они складываются
в один JSON, а страница (web/index.html) его рисует. Второго разбора состояний
на стороне браузера нет намеренно — корзина папета (free/busy/sick/silent/down)
считается здесь, и на ней же стоят счётчики в шапке.

Три расписания, потому что три цены. Ростер с состояниями приходит за
десятые доли секунды, обмер места — du по target-каталогам с сотнями тысяч
inode, расход токенов — разбор транскриптов в каждом теле. Спрашивать всё
одним кругом значило бы показывать состояние с опозданием на самый медленный
опрос. Событие на шине (mop.<шард>.events) сдвигает быстрый круг вперёд:
после `send` папет читается занятым через секунду, а не через четверть
минуты.

Модуль возвращает данные и ничего не печатает; HTTP живёт в bin/web.
"""
import threading
import time
from datetime import datetime

from . import bus, nomad, puppets, usage
from . import nodes as pool_nodes

STATES_EVERY = 15      # с: ростер Nomad + состояния с узлов
SIZES_EVERY = 120      # с: обмер du — тяжёлый IO, nice, но всё же
USAGE_EVERY = 600      # с: разбор транскриптов в телах
USAGE_DAYS = 14        # окно расхода, как у `mop stat`
USAGE_TIMEOUT = 90     # как у `mop stat`: разбор небыстрый
EVENTS_CAP = 100       # сколько событий помнит журнал
DEBOUNCE = 1.0         # с: пачка событий — один круг, а не по кругу на каждое

# Состояния, при которых папет нездоров, а не занят: те же префиксы, по
# которым doctor (puppets._action_for) назначает лечение.
SICK = ("HUNG", "needs action", "not logged in", "login expired",
        "no model quota", "error")
KINDS = ("free", "busy", "sick", "silent", "down")


# ─── чистые функции ──────────────────────────────────────────────────────
def classify(row):
    """Корзина папета по строке puppet_rows. Порядок проверок значим:
    без бегущей аллокации состояния спрашивать не у кого, что бы ни лежало
    в колонке; молчащий агент — не про папета, а про узел."""
    if row["alloc_status"] != "running":
        return "down"
    state = row["state"]
    if state.startswith("AGENT SILENT"):
        return "silent"
    if puppets.is_free(state):
        return "free"
    if state.startswith(SICK):
        return "sick"
    return "busy"


def counts(rows):
    out = {"puppets": len(rows), **{k: 0 for k in KINDS}}
    for r in rows:
        out[classify(r)] += 1
    return out


def shards(rows):
    """Строки по шардам: [{name, puppets, counts}], шарды и папеты по имени.
    Корзина кладётся в строку (`kind`), чтобы страница красила по ней."""
    by = {}
    for r in rows:
        origin = r.get("origin") or "?"
        shard = puppets.shard_of(origin) if origin != "?" else "?"
        by.setdefault(shard, []).append({**r, "kind": classify(r)})
    return [{"name": s, "puppets": sorted(ps, key=lambda r: r["name"]),
             "counts": counts(ps)} for s, ps in sorted(by.items())]


def with_sizes(rows, sizes):
    """Обмер вливается в строки по имени; кого не обмерили — None."""
    return [{**r, "disk_kb": sizes.get(r["name"])} for r in rows]


def push(journal, entry, cap=EVENTS_CAP):
    """Журнал — кольцо на cap записей, от старой к новой."""
    return (list(journal) + [entry])[-cap:]


def usage_axis(per_day, days, now=None):
    """Ось графика расхода: все дни окна, включая пустые."""
    out = []
    for d in usage.days_back(days, now=now):
        row = per_day.get(d) or {}
        out.append({"date": d, "total": usage.total(row),
                    **{k: int(row.get(k) or 0) for k in usage.KINDS}})
    return out


def snapshot(rows, nodes, issues, usage, per_puppet, journal, errors, at):
    """Один JSON на страницу и /api/pool. Набор ключей закреплён — страница
    читает их по имени."""
    return {"at": at, "shards": shards(rows), "counts": counts(rows),
            "nodes": nodes, "issues": issues, "usage": usage,
            "per_puppet": per_puppet, "journal": journal, "errors": errors}


def issue_row(issue):
    """Проблема из puppets.diagnose без объекта аллокации: странице нужны
    имя, узел, диагноз и лечение, а не весь ответ Nomad."""
    alloc = issue.get("alloc") or {}
    return {"name": issue["name"], "node": alloc.get("NodeName") or "-",
            "diagnosis": issue["diagnosis"], "action": issue.get("action")}


# ─── сборщик ─────────────────────────────────────────────────────────────
class Collector:
    """Держит снимок и три потока, которые его обновляют.

    Версия растёт на каждом изменении; `wait` отдаёт SSE-клиенту новую
    версию или таймаут. Ошибки кругов лежат в снимке по имени круга, а не
    роняют сборщик: легла шина — ростер из Nomad всё равно показывается,
    ровно как в `mop list`."""

    def __init__(self):
        self._cond = threading.Condition()
        self.version = 0
        self.rows, self.sizes, self.nodes, self.issues = [], {}, [], []
        self.usage, self.per_puppet, self.journal = [], [], []
        self.errors = {}
        self.at = None
        self._kick = threading.Event()

    # ── чтение ──
    def current(self):
        with self._cond:
            return snapshot(with_sizes(self.rows, self.sizes), self.nodes,
                            self.issues, self.usage, self.per_puppet,
                            self.journal, [f"{k}: {v}" for k, v in
                                           sorted(self.errors.items())],
                            self.at)

    def wait(self, version, timeout):
        """-> (версия, снимок) — новая версия, либо та же по таймауту."""
        with self._cond:
            self._cond.wait_for(lambda: self.version != version, timeout)
            return self.version, None if self.version == version else self.current()

    # ── запись ──
    def _bump(self):
        with self._cond:
            self.version += 1
            self._cond.notify_all()

    def event(self, entry):
        """Событие с шины: в журнал и быстрый круг вперёд."""
        with self._cond:
            self.journal = push(self.journal, {"at": time.time(), **entry})
        self._bump()
        self._kick.set()

    def kick(self):
        self._kick.set()

    def start(self):
        for fn in (self._states, self._sizes, self._usage):
            threading.Thread(target=fn, daemon=True, name=f"mop-web-{fn.__name__}").start()

    def _note(self, kind, error):
        if error is None:
            self.errors.pop(kind, None)
        else:
            self.errors[kind] = error

    def _states(self):
        while True:
            try:
                items = puppets.roster()
                rows = sorted(puppets.rows_from(items), key=lambda r: r["name"])
                nodes = pool_nodes.rows()
                issues = [issue_row(i) for i in puppets.diagnose(items)]
                with self._cond:
                    self.rows, self.nodes, self.issues = rows, nodes, issues
                    self.at = time.time()
                    self._note("states", None)
            except Exception as e:
                with self._cond:
                    self._note("states", str(e) or type(e).__name__)
                    self.at = self.at or time.time()
            self._bump()
            # Событие будит раньше срока; пачку событий гасим одной паузой.
            self._kick.wait(STATES_EVERY)
            if self._kick.is_set():
                time.sleep(DEBOUNCE)
                self._kick.clear()

    def _sizes(self):
        while True:
            with self._cond:
                rows = list(self.rows)
            if rows:
                try:
                    sizes = puppets.puppet_sizes(rows)
                    with self._cond:
                        self.sizes = sizes
                        self._note("sizes", None)
                except Exception as e:
                    with self._cond:
                        self._note("sizes", str(e) or type(e).__name__)
                self._bump()
            time.sleep(SIZES_EVERY if rows else STATES_EVERY)

    def _usage(self):
        while True:
            try:
                per_day, per_puppet = gather_usage()
                with self._cond:
                    self.usage = usage_axis(per_day, USAGE_DAYS)
                    self.per_puppet = per_puppet
                    self._note("usage", None)
            except Exception as e:
                with self._cond:
                    self._note("usage", str(e) or type(e).__name__)
            self._bump()
            time.sleep(USAGE_EVERY)


def gather_usage(days=USAGE_DAYS):
    """Расход по узлам, как в `mop stat`: -> ({дата: {вид: n}},
    [{name, node, total, вид: n}] от прожорливого к скромному)."""
    nodes = sorted(nomad.ready_nodes())
    answers = bus.request_many({n: {"verb": "usage", "days": days} for n in nodes},
                               timeout=USAGE_TIMEOUT)
    per_day, per_puppet, failed = {}, [], []
    for n in nodes:
        a = answers.get(n)
        why = bus.failure(a)
        if why:
            failed.append(f"{n}: {why}")
            continue
        for name, rows in a.get("usage", {}).items():
            t = usage.sum_days(rows)
            per_puppet.append({"name": name, "node": n, "total": usage.total(t), **t})
            usage.merge(per_day, rows)
    if nodes and len(failed) == len(nodes):
        raise RuntimeError("no node answered: " + "; ".join(failed))
    per_puppet.sort(key=lambda p: -p["total"])
    return per_day, per_puppet


def journal_entry(msg):
    """Событие с шины -> запись журнала. Поля, которых нет, — прочерк, а
    не KeyError в потоке шины."""
    return {"event": msg.get("event") or "?", "node": msg.get("node") or "-",
            "name": msg.get("name") or "-", "shard": msg.get("shard") or "-",
            "text": msg.get("text") or ""}


def human_time(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "-"
