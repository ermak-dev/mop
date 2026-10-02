"""Дашборд пула: снимок для страницы и сборщик, который его держит (#66).

Ещё один фронтенд над теми же данными, что `mop list`, `mop node` и
`mop stat`: библиотека отдаёт строки, здесь они складываются
в один JSON, а страница (React-приложение из web/src, собранное в web/dist)
его рисует. Второго разбора состояний
на стороне браузера нет намеренно — корзина папета (free/busy/sick/silent/down)
считается здесь, и на ней же стоят счётчики в шапке.

Три расписания, потому что три цены. Ростер с состояниями приходит за
десятые доли секунды, обмер места — du по target-каталогам с сотнями тысяч
inode, расход токенов — разбор транскриптов в каждом теле. Спрашивать всё
одним кругом значило бы показывать состояние с опозданием на самый медленный
опрос. Событие на шине (mop.<проект>.events) сдвигает быстрый круг вперёд:
после `send` папет читается занятым через секунду, а не через четверть
минуты.

Модуль возвращает данные и ничего не печатает; HTTP живёт в bin/web.
"""
import dataclasses
import os
import threading
import time

from ..common import bus, puppets, state
from ..common.domain import WHO_WAIT, MasterAnswer
from .. import usage
from . import nodes as node_facts

STATES_EVERY = 15      # с: ростер Nomad + состояния с узлов
SIZES_EVERY = 120      # с: обмер du — тяжёлый IO, nice, но всё же
USAGE_EVERY = 600      # с: разбор транскриптов в телах
USAGE_DAYS = 14        # окно расхода, как у `mop stat`
USAGE_TIMEOUT = 90     # как у `mop stat`: разбор небыстрый
MASTERS_EVERY = 30     # с: опрос who по проектам -- живые мастера (#305)
EVENTS_CAP = 100       # сколько событий помнит журнал
DEBOUNCE = 1.0         # с: пачка событий — один круг, а не по кругу на каждое

KINDS = ("free", "busy", "sick", "silent", "down")


# ─── чистые функции ──────────────────────────────────────────────────────
def classify(row):
    """Корзина папета по виду вердикта в строке puppet_rows. Порядок проверок
    значим: без бегущей аллокации состояния спрашивать не у кого, что бы ни
    лежало в колонке; молчащий агент — не про папета, а про узел.

    Болен тот, кому doctor назначает лечение: корзина спрашивает его самого
    (state.action_for), а не держит копию списка префиксов (#145)."""
    if row.alloc_status != "running":
        return "down"
    kind = row.kind
    if kind == "silent":
        return "silent"
    if state.is_free(kind):
        return "free"
    if state.action_for(kind):
        return "sick"
    return "busy"


def counts(rows):
    out = {"puppets": len(rows), **{k: 0 for k in KINDS}}
    for r in rows:
        out[classify(r)] += 1
    return out


# Сегменты полосы аллокаций (#378), по порядку: места бегущих папетов по
# корзинам, «прочее выделенное», свободные места. «down» мест не держит.
HELD = ("busy", "free", "sick", "silent")
SEGMENTS = (*HELD, "other", "vacant")


def load(rows, nodes):
    """Места пула и полоса аллокаций (#378). -> {total, allocated,
    free_slots, segments: [{kind, slots}] в порядке SEGMENTS}.

    Единица -- место, как в `mop node`: slots_total и slots узла (по
    памяти, cluster.nomad_pool). Узел без мест (down, отказ -- None) в сумму
    не входит. Места выделенного раздаются корзинам бегущих папетов по
    порядку HELD, остаток -- «прочее» (чужие джобы, просьбы проектов о
    памяти больше spec.MEM). Папетов больше, чем выделено мест (папет
    попросил меньше spec.MEM, его узел без ёмкости), -- места кончаются по
    порядку HELD, хвост не рисуется: сумма сегментов -- всего мест, а
    честные числа корзин -- в counts."""
    sized = [n for n in nodes
             if isinstance(n.get("slots"), int) and isinstance(n.get("slots_total"), int)]
    total = sum(n["slots_total"] for n in sized)
    free_slots = sum(n["slots"] for n in sized)
    allocated = total - free_slots
    by = counts(rows)
    left, segments = allocated, []
    for k in HELD:
        take = min(by[k], left)
        segments.append({"kind": k, "slots": take})
        left -= take
    segments += [{"kind": "other", "slots": left}, {"kind": "vacant", "slots": free_slots}]
    return {"total": total, "allocated": allocated, "free_slots": free_slots,
            "segments": segments}


def row_project(r):
    """Проект строки ростера; без origin -- «?», как и показ строки
    (PuppetRow.render). Одно место на снимок и секцию мастеров (#319)."""
    return puppets.project_of(r.origin) if r.origin else "?"


def error_text(e):
    """Исключение -> строка для страницы: текст, а пустой -- имя типа. Одна
    на круги сборщика и отказы обработчика страницы (#319)."""
    return str(e) or type(e).__name__


def projects(rows):
    """Строки по проектам: [{name, puppets, counts}], проекты и папеты по имени.
    Корзина кладётся в строку (`kind`) поверх вида вердикта, чтобы страница
    красила по ней: самой странице вид не нужен, и разбирать она не должна.
    Здесь строка ростера и становится JSON страницы (#204): to_dict -- одно
    место на весь снимок."""
    by = {}
    for r in rows:
        by.setdefault(row_project(r), []).append(r)
    # Счётчик проекта -- тот же counts по виду вердикта, что и в шапке
    # (#210): считать по строке, где kind уже заменён корзиной, значило
    # отвечать classify("sick") -> busy, и залипший папет прятался в занятых.
    return [{"name": s,
             "puppets": [{**r.to_dict(), "kind": classify(r)}
                         for r in sorted(ps, key=lambda r: r.name)],
             "counts": counts(ps)} for s, ps in sorted(by.items())]


def with_sizes(rows, sizes):
    """Обмер вливается в строки по имени; кого не обмерили — None."""
    return [dataclasses.replace(r, disk_kb=sizes.get(r.name)) for r in rows]


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


def user_rows(answers):
    """Ответы узлов на usage -> [{login, вид: n, total}] от самого
    прожорливого (#245). Не ответивший узел в строки не входит -- его
    отказ уже в ошибках круга; узел со старым агентом -- весь в «-»."""
    acc = {}
    for a in answers.values():
        if bus.failure(a):
            continue
        usage.add_users(acc, usage.by_user(a))
    rows = [{"login": login, **{k: t[k] for k in usage.KINDS}, "total": usage.total(t)}
            for login, t in acc.items()]
    return sorted(rows, key=lambda r: (-r["total"], r["login"]))


def master_rows(answers, rows):
    """Ответы who по проектам -> строки секции «Мастера» (#305). Чистая.

    answers -- {проект: [ответ who]}, ответ -- то, что отдаёт mcp.on_inbox:
    {master, project, user, session, cwd}. Реестра мастеров нет намеренно
    (mcp._masters): живой -- тот, кто отозвался. Папеты -- проекта, чья
    аренда (owner строки ростера) на логине мастера: так видно, кто чем
    правит. Один мастер, ответивший дважды, -- одна строка."""
    by_owner = {}
    for r in rows:
        if r.owner:
            by_owner.setdefault((row_project(r), r.owner), []).append(r.name)
    seen, out = set(), []
    for asked, found in answers.items():
        for d in found or []:
            a = MasterAnswer.from_dict(d)
            project = a.project or asked
            address = str(a.master or "-")
            if (project, address) in seen:
                continue
            seen.add((project, address))
            user = a.user or "-"
            out.append({"project": project, "master": address, "user": user,
                        "session": a.session or "-", "cwd": a.cwd or "-",
                        "puppets": sorted(by_owner.get((project, user), []))})
    return sorted(out, key=lambda m: (m["project"], m["user"], m["master"]))


def snapshot(rows, nodes, usage, per_puppet, per_user, journal, errors, at,
             masters=()):
    """Один JSON на страницу и /api/pool. Набор ключей закреплён — страница
    читает их по имени.

    Диагностики здесь нет (решение оператора 2026-09-22): она стоила
    запроса к Nomad на каждого папета каждым кругом, а лечение всё равно
    остаётся за `mop server doctor`; больной папет и так виден корзиной sick.
    creds -- строки реестра кредитов (#285), уже без секретов (cred_rows);
    masters -- живые мастера по опросу who (#305, master_rows); load --
    места пула (#378) по nodes, строкам node_rows."""
    return {"at": at, "projects": projects(rows), "counts": counts(rows),
            "nodes": nodes, "usage": usage,
            "per_puppet": per_puppet, "per_user": per_user,
            "journal": journal, "errors": errors,
            "masters": list(masters),
            # Период опроса мастеров (#325): страница пишет «раз в N с» по
            # нему, а не своей копией числа.
            "masters_every": MASTERS_EVERY,
            # Места пула и полоса аллокаций (#378): суммы и сегменты
            # считает сервер, страница только рисует.
            "load": load(rows, nodes)}


def node_rows(rows):
    """Строки узлов для снимка: прежние ключи плюс kind -- корзина
    free / busy / down (#325) по правилу nodes.bucket, чтобы страница не
    разбирала строку state регэкспами."""
    return [{**r, "kind": node_facts.bucket(r.get("state") or "")} for r in rows]


# ─── собранное приложение (#297) ─────────────────────────────────────────
# Страница -- React-приложение, собранное Vite в web/dist и закоммиченное:
# на серверах node нет, а rumop раскатывается с сервера вручную. Сервис
# отдаёт index.html и файлы под assets/ с именами, в которых хеш содержимого,
# поэтому ассеты кэшируются на год, а индекс -- никогда (он и называет
# свежие хеши). Из каталога отдаётся ровно одно плоское имя: ни подкаталогов,
# ни `..`, ни скрытых файлов -- обход каталога здесь невозможен по построению.
ASSETS = "/assets/"
CONTENT_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml", ".woff2": "font/woff2", ".woff": "font/woff",
                 ".png": "image/png", ".map": "application/json",
                 ".json": "application/json; charset=utf-8", ".ico": "image/x-icon"}


def asset_path(url, root):
    """Путь файла ассета под root по пути запроса, либо None: только
    /assets/<имя> с одним плоским именем без точки в начале и без слешей."""
    if not url.startswith(ASSETS):
        return None
    name = url[len(ASSETS):]
    if not name or "/" in name or "\\" in name or "%" in name or name.startswith(".") \
            or name in (".", ".."):
        return None
    return os.path.join(root, "assets", name)


def content_type(name):
    """Тип по расширению; незнакомое -- поток байтов."""
    return CONTENT_TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


def cache_control(url):
    """Ассеты с хешем в имени -- на год, всё остальное (индекс) -- не кэшировать."""
    return "public, max-age=31536000, immutable" if url.startswith(ASSETS) else "no-cache"


# ─── реестр кредитов на странице (#285) ──────────────────────────────────
# Секция без входа: решение оператора 26.09, LAN доверенная, авторизация
# действий -- позже отдельным тикетом. Секреты в снимок не попадают никогда:
# строка собирается из перечисленных полей, а не копией записи.
CRED_WORDS = {"active": "активен", "quota_wait": "ждёт квоты",
              "needs_login": "ждёт ручной авторизации"}


def _body(body):
    """Тело POST -> (dict, None) либо (None, причина)."""
    if not isinstance(body, dict):
        return None, "body: a JSON object is expected"
    return body, None


def _field(body, name, required=True):
    """Строковое поле тела, обрезанное; пустое обязательное -- отказ именем поля."""
    v = body.get(name)
    v = v.strip() if isinstance(v, str) else ""
    if required and not v:
        return None, f"{name}: required"
    return v, None


class Collector:
    """Держит снимок и пять потоков, которые его обновляют: states, sizes,
    usage, masters (start).

    Версия растёт на каждом изменении; `wait` отдаёт SSE-клиенту новую
    версию или таймаут. Ошибки кругов лежат в снимке по имени круга, а не
    роняют сборщик: легла шина — ростер из Nomad всё равно показывается,
    ровно как в `mop list`."""

    def __init__(self):
        self._cond = threading.Condition()
        self.version = 0
        self.rows, self.sizes, self.nodes = [], {}, []
        self.usage, self.per_puppet, self.per_user, self.journal = [], [], [], []
        self.masters = []
        self.errors = {}
        self.at = None
        self._kick = threading.Event()

    # ── чтение ──
    def current(self):
        with self._cond:
            return snapshot(with_sizes(self.rows, self.sizes), self.nodes,
                            self.usage, self.per_puppet, self.per_user,
                            self.journal, [f"{k}: {v}" for k, v in
                                           sorted(self.errors.items())],
                            self.at, self.masters)

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
        for fn in (self._states, self._sizes, self._usage, self._masters):
            threading.Thread(target=fn, daemon=True, name=f"mop-web-{fn.__name__}").start()

    def _note(self, kind, error):
        if error is None:
            self.errors.pop(kind, None)
        else:
            self.errors[kind] = error

    def _round(self, kind, compute, apply, failed=None):
        """Один круг сборщика (#319): compute() -- вне замка (шина, Nomad,
        секунды), apply(что вышло) и заметка -- под замком; отказ -- заметка
        с текстом ошибки и failed() под тем же замком; версия -- после.
        Прежде это было написано пять раз, и заметка у каждого круга своя."""
        try:
            got = compute()
            with self._cond:
                apply(got)
                self._note(kind, None)
        except Exception as e:
            with self._cond:
                self._note(kind, error_text(e))
                if failed:
                    failed()
        self._bump()

    def _states(self):
        def compute():
            # Страница читает ключи строки nodes по имени: наружу --
            # прежняя форма провода (#267).
            return puppets.puppet_rows(sizes=False), node_rows(n.to_row() for n in puppets.nodes())

        def apply(got):
            self.rows, self.nodes = got
            self.at = time.time()

        def failed():
            self.at = self.at or time.time()
        while True:
            self._round("states", compute, apply, failed)
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
                self._round("sizes", lambda: puppets.puppet_sizes(rows),
                            lambda sizes: setattr(self, "sizes", sizes))
            time.sleep(SIZES_EVERY if rows else STATES_EVERY)

    def _usage(self):
        def apply(got):
            per_day, self.per_puppet, self.per_user = got
            self.usage = usage_axis(per_day, USAGE_DAYS)
        while True:
            self._round("usage", gather_usage, apply)
            time.sleep(USAGE_EVERY)


    def _masters(self):
        # Живые мастера (#305): опрос who в общий инбокс каждого проекта --
        # тот же, которым их находит инструмент agents. Проекты -- реестр
        # сервера (и проекты без папетов) плюс проекты ростера. service
        # публиковать туда вправе (mop.> без rpc), ответы -- в _INBOX.
        from ..common import busnames, projects as registry

        def compute():
            with self._cond:
                rows = list(self.rows)
            names = set(registry.names(*puppets.project_ids(registry.read())))
            names |= {puppets.project_of(r.origin) for r in rows if r.origin}
            answers = {p: bus.gather("who", timeout=WHO_WAIT,
                                     subj=busnames.inbox(p, busnames.ALL_MASTERS))
                       for p in sorted(names)}
            return master_rows(answers, rows)
        while True:
            self._round("masters", compute, lambda found: setattr(self, "masters", found))
            time.sleep(MASTERS_EVERY)
def gather_usage(days=USAGE_DAYS):
    """Расход по узлам, как в `mop stat`: -> ({дата: {вид: n}},
    [{name, node, total, вид: n}] от прожорливого к скромному,
    [{login, вид: n, total}] по людям, #245)."""
    nodes = sorted(puppets.ready_nodes())
    answers = bus.request_many("usage", nodes, days=days, timeout=USAGE_TIMEOUT)
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
    return per_day, per_puppet, user_rows(answers)


def journal_entry(msg):
    """Событие с шины -> запись журнала. Поля, которых нет, — прочерк, а
    не KeyError в потоке шины."""
    return {"event": msg.get("event") or "?", "node": msg.get("node") or "-",
            "name": msg.get("name") or "-", "project": msg.get("project") or "-",
            "text": msg.get("text") or ""}


