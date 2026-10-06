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
import json
import re
import os
import threading
import time

from ..common import bus, paths, puppets, state
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
    Корзина -- отдельным ключом bucket (#452): вид вердикта (kind) остаётся
    собой, страница красит по корзине и рисует статус по виду, не разбирая
    текст состояния. Здесь строка ростера и становится JSON страницы
    (#204): to_dict -- одно место на весь снимок."""
    by = {}
    for r in rows:
        by.setdefault(row_project(r), []).append(r)
    # Счётчик проекта -- тот же counts по виду вердикта, что и в шапке
    # (#210): считать по строке, где kind заменён корзиной, значило
    # отвечать classify("sick") -> busy, и залипший папет прятался в занятых.
    return [{"name": s,
             "puppets": [{**r.to_dict(), "bucket": classify(r)}
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
    """Ответы who по проектам -> строки секции «Мастера» (#305, #421). Чистая.

    answers -- {проект: [ответ who]}, ответ -- то, что отдаёт mcp.on_inbox:
    {master, project, user, session, sessionId, cwd}. Реестра мастеров нет
    намеренно (mcp._masters): живой -- тот, кто отозвался.

    Единица строки -- живая Claude-сессия (#421), не who-адрес: у сессии
    адресов несколько (личный инбокс и общий проекта), и адрес-единица
    плодил строки и приписывал папетов каждому адресу. sessionId -- имя
    файла сессии, оно уникально; все адреса сессии собираются списком, ни
    один не теряется. Ответ без sessionId (#421 -- старый мастер) склеить
    по сессии нельзя: дедуп по самому (project, address) -- один ответивший
    endpoint, одна строка, как и до #421.

    «puppets» -- папеты проекта, чья аренда (owner строки ростера) на
    логине: принадлежности сессии оно не выражает, поле оставлено для
    страницы, ownership пояснит #423. Один мастер, ответивший дважды, --
    одна строка."""
    by_owner = {}
    for r in rows:
        if r.owner:
            by_owner.setdefault((row_project(r), r.owner), []).append(r.name)
    by_session, order = {}, []
    for asked, found in answers.items():
        for d in found or []:
            a = MasterAnswer.from_dict(d)
            project = a.project or asked
            address = str(a.master or "-")
            # Ключ -- сессия; без sessionId у ответа нет личности, и каждая
            # такая строка своя: счётчик в ключе не даёт им склеиться.
            # Ключ -- сессия. Без sessionId личности нет: дедуп -- по самому
            # (project, address), один ответивший endpoint -- одна строка
            # (так было и до #421); разные адреса -- разные строки.
            key = (project, a.session_id) if a.session_id else (project, address)
            if key not in by_session:
                by_session[key] = {"project": project, "master": address,
                                   "addresses": [address], "user": a.user or "-",
                                   "session": a.session or "-", "cwd": a.cwd or "-",
                                   # Чем строка склеена (#421): сессией (у who-
                                   # ответа есть sessionId) или самим адресом
                                   # (legacy endpoint). Число «сессий» на
                                   # странице без этого врёт, пока живы старые
                                   # MCP; сам sessionId публично не ходит.
                                   "identified": bool(a.session_id)}
                order.append(key)
            elif address not in by_session[key]["addresses"]:
                by_session[key]["addresses"].append(address)
    out = []
    for key in order:
        m = dict(by_session[key])
        m["puppets"] = sorted(by_owner.get((m["project"], m["user"]), []))
        out.append(m)
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
            # Строка мастера -- живая сессия (#421): addresses -- все её
            # who-адреса, master -- первый из них (адресация старой страницы).
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
        self.admin_journal = []     # полный журнал, только /api/journal (#424)
        self.masters = []
        self.errors = {}
        self.at = None
        self._kick = threading.Event()

    # ── чтение ──
    def current(self):
        with self._cond:
            return snapshot(with_sizes(self.rows, self.sizes), self.nodes,
                            self.usage, self.per_puppet, self.per_user,
                            # Публичный снимок без записей журнала (#424):
                            # полный -- только /api/journal под admin-ролью.
                            [], [f"{k}: {v}" for k, v in
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

    def event(self, entry, msg=None):
        """Событие с шины: полный журнал -- админу (#424), быстрый круг --
        вперёд. Публичный снимок записей больше не несёт: пустой список на
        переход (#424), живое -- в admin_journal."""
        full = full_entry(msg if msg is not None else entry)
        with self._cond:
            self.admin_journal = push(getattr(self, "admin_journal", []), full)
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


# ─── гейтвей управления прокси (#417) ────────────────────────────────────
# Write-guard конфига CLIProxyAPI: сток-панель в Source-режиме кладёт весь
# документ PUT'ом и без блока management убивает ключ; PATCH с полем
# secret-key подменяет ключ значением из браузера. Решение принимает
# сервер -- пропустить, отказать или инжектировать management-блок из
# серверного источника; парсинг только здесь, сырой ключ наружу не идёт.
import yaml


class KeyBreaker:
    """Circuit breaker ключа management (#417): CPA банит петлю на 30 минут
    после пяти неудач, а панель одним загрузом шлёт больше пяти запросов.
    После upstream 401/403 ключ считается отвергнутым: все запросы с ним
    получают 503 локально, без похода к upstream и без ключа в ответе --
    пока содержимое защищённой копии не сменится (новое значение -- новая
    проба). Ротация ключа -- отдельная операция оператора, не автоматизм."""

    REFUSED_BODY = b"proxy management key rejected"
    # Бан CPA петлевой: 30 минут, и ротация ключа его не снимает
    COOLDOWN = 30 * 60

    def __init__(self, ask=None):
        self._lock = threading.Lock()
        self._refused = set()
        self._banned_until = 0.0
        self._proven = set()     # ключи с подтверждённым успехом
        self._inflight = {}      # ключ -> событие «проба завершена»
        self._ask = ask          # диагностика проверок: каждый локальный отказ

    def __call__(self, method, path, body, key, upstream):
        """Пропустить запрос к upstream, если ключ жив. -> (код, тело).

        Неподтверждённый ключ -- single-flight: первая проба идёт одна,
        остальные ждут её исхода. Панель открывает запросы пачкой, и без
        этого пять параллельных 401 банят петлю раньше, чем мы узнаем об
        отказе. Подтверждённый успех снимает сериализацию."""
        while True:
            with self._lock:
                refused = key in self._refused or time.time() < self._banned_until
                if refused:
                    if self._ask:
                        self._ask()
                    return 503, self.REFUSED_BODY
                if key in self._proven:
                    break
                done = self._inflight.get(key)
                if done is None:
                    done = threading.Event()
                    self._inflight[key] = done
                    break
            done.wait()
        code = None
        try:
            code, got = upstream(method, path, body, key)
        finally:
            # Изменить состояние до освобождения ожидающих: если event.set()
            # случится раньше отказа 401, второй поток сделает вторую пробу.
            with self._lock:
                if code == 200:
                    self._proven.add(key)
                if code in (401, 403):
                    self._refused.add(key)
                    if code == 403:
                        # Бан CPA петлевой: ротация не снимает cooldown.
                        self._banned_until = max(self._banned_until,
                                                 time.time() + self.COOLDOWN)
                event = self._inflight.pop(key, None)
                if event:
                    event.set()
        if code in (401, 403):
            return code, got
        return code, got


# Management-блок, инжектируемый полному PUT (#417, вариант A): ключ
# перечитывается из закрытого файла на каждый write, остальные поля --
# безопасные дефолты управления (в config.yaml прокси их задаёт deploy).
MANAGEMENT_DEFAULTS = {"allow-remote": False, "disable-auto-update-panel": True}


def _management_paths_hit(obj, key=None):
    """Затрагивает ли JSON-запись ЯВНОЕ поле management.secret-key: новый
    ключ -- замена, плейсхолдер или хэш текущего ключа из прочитанного
    конфига (#523) -- как отсутствие поля (браузер вернул то, что мы
    ему показали)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "secret-key":
                if isinstance(v, str) and (v == PLACEHOLDER
                                           or _echoed_current_key(v, key)):
                    return False
                return True
            if k == "management" and isinstance(v, dict) and "secret-key" in v:
                got = v["secret-key"]
                if not isinstance(got, str):
                    return True
                return not (got == PLACEHOLDER
                            or _echoed_current_key(got, key))
    return False


def _yaml_doc(body):
    """YAML-тело -> dict | None (битое/не словарь -- None)."""
    try:
        doc = yaml.safe_load(body)
    except yaml.YAMLError:
        return None
    return doc if isinstance(doc, dict) else {}


def _has_explicit_secret(doc, key=None):
    """В документе есть ЯВНЫЙ secret-key (не наш плейсхолдер из чтения и
    не хэш текущего ключа, echoing назад панелью -- #523)?"""
    mgmt = doc.get("management")
    if not isinstance(mgmt, dict) or "secret-key" not in mgmt:
        return False
    value = mgmt["secret-key"]
    if isinstance(value, str) and (value == PLACEHOLDER
                                   or _echoed_current_key(value, key)):
        return False
    return True


def _yaml_has_management(body):
    try:
        doc = yaml.safe_load(body)
    except yaml.YAMLError:
        return None
    return isinstance(doc, dict) and isinstance(doc.get("management"), dict)


# Что видит браузер вместо хэша в прочитанном конфиге (#417): плейсхолдер
# не секрет и знаком guard'у -- его возврат в write читается как отсутствие
# поля, не как явная замена ключа.
PLACEHOLDER = "managed-by-mop"


def _echoed_current_key(value, key):
    """Значение secret-key -- bcrypt-хэш ТЕКУЩЕГО ключа сервера (#523)?

    Сток-панель показывает прочитанный конфиг и при сохранении возвращает
    прочитанные поля как есть: upstream отдаёт ключ хэшем, и панель несёт
    его обратно. Такой хэш -- то же, что плейсхолдер: браузер вернул то,
    что ему показали, нового ключа в записи нет. Сверка -- bcrypt по
    ключу из закрытой копии (он у guard уже есть); без bcrypt или без
    ключа хэш не распознаётся, и запись остаётся 403 -- молчаливого
    послабления guard'а нет. Новый ключ (значение мимо сверки) -- 403,
    как было (#417: ключ ведёт mop)."""
    if not isinstance(value, str) or not value or value == PLACEHOLDER or not key:
        return False
    if not (value.startswith("$2a$") or value.startswith("$2b$")
            or value.startswith("$2y$")):
        return False
    try:
        import bcrypt
    except ImportError:
        return False
    try:
        return bcrypt.checkpw(key.encode(), value.encode())
    except (ValueError, TypeError):
        return False
# Sentinel для шима сток-панели (#417): публичное слово в localStorage
# браузера; upstream его не принимает, гейтвей подменяет своим ключом.
SENTINEL = "mop-proxy-managed"
# Размеры гейтвея (#417): сток-панель -- один файл ~3 МБ; тела write --
# конфиги, но лимит общий с /llm/ у nginx.
PANEL_MAX = 4 * 1024 * 1024
REDIRECT_MAX = 3


def mgmt_key_path():
    """Закрытая копия management-ключа (#418): каталог пула текущего
    пользователя, никакие хосты/home вне paths.local. До раскатки #418
    файла нет -- гейтвей fail closed, и это норма перехода."""
    return paths.local(paths.SECRETS, "llm-proxy-mgmt.pass")


# Что из заголовков ответа upstream уходит браузеру (#417): cookie и
# аутентификационные вызовы прокси не проходят -- сессия у нас своя.
RESPONSE_KEEP = ("Content-Type", "X-Cpa-Version", "X-Cpa-Build-Date",
                 "X-Server-Version", "X-Cpa-Support-Plugin")


def response_headers(headers):
    """Заголовки upstream -> заголовки для браузера: только whitelist."""
    return {k: v for k, v in (headers or {}).items()
            if k in RESPONSE_KEEP}


def redact_management_read(path, body):
    """Ответ upstream -> тело для браузера: хэш secret-key в /config и
    /config.yaml закрыт плейсхолдером, остальное -- байт в байт. Не-конфиг
    и битые тела проходят как есть: чтение не должно рвать страницу."""
    if not path.rstrip("/").endswith("/config") and not path.endswith("/config.yaml"):
        return body
    try:
        if path.endswith("/config.yaml"):
            doc = yaml.safe_load(body)
            if isinstance(doc, dict) and isinstance(doc.get("management"), dict) \
                    and "secret-key" in doc["management"]:
                doc["management"]["secret-key"] = PLACEHOLDER
                return yaml.safe_dump(doc, default_flow_style=False,
                                      allow_unicode=True).encode()
            return body
        doc = json.loads(body)
        if isinstance(doc, dict) and isinstance(doc.get("management"), dict) \
                and "secret-key" in doc["management"]:
            doc["management"]["secret-key"] = PLACEHOLDER
            return json.dumps(doc, ensure_ascii=False).encode()
        return body
    except (ValueError, UnicodeDecodeError, yaml.YAMLError):
        return None     # битый ответ с секретом нельзя выдавать как есть


def guard_management_write(method, path, body, key_source):
    """Решение write-guard'а. -> {action: pass|refuse|inject, status?,
    body?}.

    method/path -- запрос браузера; body -- байты; key_source -- callable
    -> plaintext-ключ из закрытого файла сервера | None (файл недоступен);
    зовётся на каждый write, ротация подхватывается без кэша. Отказ: 403
    «поле ведётся mop'ом», 400 «битое тело», 503 «ключ недоступен, а PUT
    удалил бы секрет». Инжект: management-блок получает актуальный
    secret-key; прочие management-настройки клиента сохраняются, полей
    без клиента добавляются безопасные дефолты. Сырой ключ живёт только
    в теле исходящего loopback-запроса -- не в ответах и не в логах."""
    if method not in ("POST", "PATCH", "PUT", "DELETE"):
        return {"action": "pass"}
    if not path.startswith("/v8/management/"):
        return {"action": "pass"}

    is_yaml = path.endswith("/config.yaml")
    if not (path.rstrip("/").endswith("/config") or is_yaml
            or "/config/" in path):
        return {"action": "pass"}     # не конфиг: credentials, oauth и пр.

    # Полный документ нельзя удалять/создавать без preservation-контракта;
    # панель сохраняет его PUT, отдельные настройки -- PATCH.
    if method in ("POST", "DELETE"):
        return {"action": "refuse", "status": 403}

    # path-addressed запись в само поле
    if "/config/management" in path and method == "PATCH":
        return {"action": "refuse", "status": 403}


    if is_yaml:
        doc = _yaml_doc(body)
        if doc is None:
            return {"action": "refuse", "status": 400}
        # Ключ нужен уже для распознавания хэша (#523): панель возвращает
        # прочитанное значение, и хэш текущего ключа -- «нет поля», не 403.
        key = key_source()
        has_secret = _has_explicit_secret(doc, key)
        has_mgmt = isinstance(doc.get("management"), dict)
        if has_secret:
            return {"action": "refuse", "status": 403}
        if key is None:
            return {"action": "refuse", "status": 503}
        if not has_mgmt:
            doc["management"] = {"secret-key": key, **MANAGEMENT_DEFAULTS}
        else:
            # блок клиента без явного ключа (или с нашим плейсхолдером,
            # или с хэшем текущего ключа) -- ключ сервера, прочие
            # настройки клиента сохраняются
            client = {k: v for k, v in doc["management"].items()
                      if k != "secret-key"}
            doc["management"] = {**MANAGEMENT_DEFAULTS, **client,
                                 "secret-key": key}
        return {"action": "inject",
                "body": yaml.safe_dump(doc, default_flow_style=False,
                                       allow_unicode=True).encode()}

    # JSON-ветка: PATCH -- слияние, PUT -- полная замена
    try:
        doc = json.loads(body or b"{}")
        if not isinstance(doc, dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        return {"action": "refuse", "status": 400}
    key = key_source()
    if _management_paths_hit(doc, key):
        return {"action": "refuse", "status": 403}
    # плейсхолдер или хэш текущего ключа из прочитанного конфига (#523) --
    # как отсутствие поля
    mgmt = doc.get("management")
    if isinstance(mgmt, dict) and isinstance(mgmt.get("secret-key"), str) \
            and (mgmt["secret-key"] == PLACEHOLDER
                 or _echoed_current_key(mgmt["secret-key"], key)):
        mgmt.pop("secret-key")
    if method == "PUT":
        key = key_source()
        if key is None:
            return {"action": "refuse", "status": 503}
        client = doc.get("management") if isinstance(doc.get("management"), dict) else {}
        # настройки клиента сохраняются, ключ -- только серверный
        doc["management"] = {**MANAGEMENT_DEFAULTS, **client,
                             "secret-key": key}
        return {"action": "inject",
                "body": json.dumps(doc, ensure_ascii=False).encode()}
    return {"action": "pass"}


# ─── полный журнал для админа (#424) ─────────────────────────────────────
# Одна запись -- не больше ENTRY_MAX байт сериализации: событие больше
# заменяется маркером, а не хранится oversized.
ENTRY_MAX = 64 * 1024
# Поля-ключи не хранятся и не отдаются: событие с шины публичных данных не
# обещает, а журнал читает человек и браузер. Регистр не важен, вложенные
# словари чистятся тем же списком. proxy_key -- ключ LLM-прокси установки
# (#391): имя его переменной MOP_PROXY_KEY не содержит ни token, ни secret,
# и без отдельного слова ключ ездил бы в событиях и хвостах как есть (#462).
CREDENTIAL_KEYS = ("management_key", "authorization", "password", "passwd",
                   "token", "secret", "api_key", "access_key", "credentials",
                   "proxy_key")


def _valid_at(value):
    """Исходный at события -> число | None: bool и нечисла (NaN, inf,
    строки) не годятся -- JSON их не переносит и смысл у них другой."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


# Типовой секрет в свободном тексте события (#424, доработка): значение
# bearer-схемы. Только эта форма -- произвольный текст не распознаётся как
# секрет, и обещать этого нельзя; остальной текст остаётся как был.
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
# Ключ=значение в тексте команды (#424, follow-up): значение теряется, слово
# ключа остаётся. Словарь -- тот же CREDENTIAL_KEYS (признанный список), а
# не «любое слово перед =»: произвольная строка секретом не считается. Формы:
# --key value, --key=value, key=value, key: value (регистр ключа не важен).
_KEY = "|".join(re.escape(k) for k in sorted(CREDENTIAL_KEYS, key=len, reverse=True))
_KV = re.compile(rf"(?i)(--)?({_KEY})([=:\s]+)([^\s,;]+)")


def _redact_text(text):
    """Убрать bearer-значения и ключ=значение из строки события; контекст
    вокруг -- оставить."""
    if not isinstance(text, str):
        return text
    def kv(m):
        # Дефис-префикс и слово ключа остаются: контекст команды читается.
        return f"{m.group(1) or ''}{m.group(2)}{m.group(3)}[redacted]"
    return _KV.sub(kv, _BEARER.sub("Bearer [redacted]", text))


def _strip_credentials(value):
    """Убрать credential-поля на любом уровне. Списки чистятся поэлементно,
    словари -- по ключу (без учёта регистра); остальное -- как есть."""
    if isinstance(value, dict):
        return {k: _strip_credentials(v)
                for k, v in value.items()
                if not any(k.lower().endswith(s) or s in k.lower() for s in CREDENTIAL_KEYS)}
    if isinstance(value, list):
        return [_strip_credentials(v) for v in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def full_entry(msg, now=None):
    """Событие шины -> полная запись админского журнала (#424).

    Все обычные поля события сохраняются без белого списка; исходный at
    остаётся, если это валидное число, иначе -- время получения (fail к
    честной метке, не null); received_at -- всегда отдельное поле.
    Credential-поля вычищаются; событие, не влезающее в ENTRY_MAX байт
    сериализации, заменяется ограниченным маркером."""
    received = time.time() if now is None else (now() if callable(now) else now)
    clean = _strip_credentials(dict(msg))
    at = _valid_at(clean.pop("at", None))
    entry = {"at": at if at is not None else received, "received_at": received, **clean}
    if len(json.dumps(entry, ensure_ascii=False, default=str).encode()) > ENTRY_MAX:
        # Маркер ограничен целиком: даже имя события не переносится сырым --
        # длинное имя само раздувает запись и может нести секрет. Короткое --
        # отредактированным (в clean -- bearer и ключ=значение уже сняты),
        # не сырым: 64 байта хватает и на «Bearer sk-…».
        name = clean.get("event")
        name = name if isinstance(name, str) and len(name) <= 64 else "?"
        return {"event": name, "text": "[event over size limit]",
                "at": entry["at"], "received_at": received}
    return entry




# ── карточка папета и его хвост (#462) ───────────────────────────────────
# Приватные read-only маршруты дашборда: гость не входит, user не admin,
# цель -- РЕАЛЬНЫЙ job из строк ростера (имя из пути до читателей не
# доезжает: неизвестное -- 404, а не запрос к шине). Читатели -- молчаливые
# функции puppets (pane_lines, alloc_stderr, node_of): HTTP-слой их только
# зовёт, ничего не парся из CLI. Лимиты -- константы сервера, обрезание
# помечается честно. Мутирующих глаголов здесь нет вовсе.
TAIL_LINES = 1000        # строк панеля: tmux-история бывает огромной
TAIL_TIMEOUT = 30        # с на панель: медленный du не должен рвать ответ


def puppet_card(rows, name):
    """Строка ростера по имени -> PuppetRow | None. Чистая функция.

    Цель ищется по РЕАЛЬНЫМ строкам (Collector.rows), не по префиксу пути и
    не по запросу шины: имя, которого нет в ростере, -- отсутствующая цель,
    а не чужая. Контракт статуса -- #452, как есть: слово по kind, detail,
    branch -- уже в строке."""
    for r in rows or []:
        if r.name == name:
            return r
    return None


def puppet_tail(name, pane, node_of):
    """Хвост экрана папета -> (строки, обрезано) | (None, причина).

    pane/node_of -- читатели puppets (подмена в проверках): узел берётся у
    работающего job'а, панель -- целиком и режется с конца до лимита;
    обрезание помечается, молчать о нём -- врать оператору. Каждая строка
    проходит _redact_text (#424): экран показывает ключи (экспорт, curl с
    bearer), и хвост не должен увозить их в браузер."""
    try:
        node = node_of(name)
    except (LookupError, RuntimeError, Exception) as e:
        return None, f"node unknown: {e}"
    try:
        lines = pane(node, name)
    except Exception as e:  # читатель умер -- честный отказ, не пустота
        return None, f"reader failed: {e}"
    if len(lines) <= TAIL_LINES:
        return [_redact_text(l) for l in lines], False
    return [_redact_text(l) for l in lines[-TAIL_LINES:]], True

