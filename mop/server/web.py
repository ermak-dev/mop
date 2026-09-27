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

from ..common import bus, credreg as credrows, puppets, state
from .. import usage

STATES_EVERY = 15      # с: ростер Nomad + состояния с узлов
SIZES_EVERY = 120      # с: обмер du — тяжёлый IO, nice, но всё же
USAGE_EVERY = 600      # с: разбор транскриптов в телах
USAGE_DAYS = 14        # окно расхода, как у `mop stat`
USAGE_TIMEOUT = 90     # как у `mop stat`: разбор небыстрый
CREDS_EVERY = 60       # с: реестр кредитов -- чтение файлов на сервере (#285)
MASTERS_EVERY = 30     # с: опрос who по проектам -- живые мастера (#305)
MASTERS_WAIT = 2       # с: сколько ждать ответов who на проект
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


def projects(rows):
    """Строки по проектам: [{name, puppets, counts}], проекты и папеты по имени.
    Корзина кладётся в строку (`kind`) поверх вида вердикта, чтобы страница
    красила по ней: самой странице вид не нужен, и разбирать она не должна.
    Здесь строка ростера и становится JSON страницы (#204): to_dict -- одно
    место на весь снимок."""
    by = {}
    for r in rows:
        # Без origin -- проект «?», как и показ строки (PuppetRow.render).
        project = puppets.project_of(r.origin) if r.origin else "?"
        by.setdefault(project, []).append(r)
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
        project = puppets.project_of(r.origin) if r.origin else "?"
        if r.owner:
            by_owner.setdefault((project, r.owner), []).append(r.name)
    seen, out = set(), []
    for asked, found in answers.items():
        for d in found or []:
            project = d.get("project") or asked
            address = str(d.get("master") or "-")
            if (project, address) in seen:
                continue
            seen.add((project, address))
            user = d.get("user") or "-"
            out.append({"project": project, "master": address, "user": user,
                        "session": d.get("session") or "-", "cwd": d.get("cwd") or "-",
                        "puppets": sorted(by_owner.get((project, user), []))})
    return sorted(out, key=lambda m: (m["project"], m["user"], m["master"]))


def snapshot(rows, nodes, usage, per_puppet, per_user, journal, errors, at, creds=(),
             masters=()):
    """Один JSON на страницу и /api/pool. Набор ключей закреплён — страница
    читает их по имени.

    Диагностики здесь нет (решение оператора 2026-09-22): она стоила
    запроса к Nomad на каждого папета каждым кругом, а лечение всё равно
    остаётся за `mop doctor`; больной папет и так виден корзиной sick.
    creds -- строки реестра кредитов (#285), уже без секретов (cred_rows);
    masters -- живые мастера по опросу who (#305, master_rows)."""
    return {"at": at, "projects": projects(rows), "counts": counts(rows),
            "nodes": nodes, "usage": usage,
            "per_puppet": per_puppet, "per_user": per_user,
            "journal": journal, "errors": errors, "creds": list(creds),
            "masters": list(masters)}


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
CRED_FIELDS = ("name", "profile", "kind", "owner", "status", "resets_at", "percent", "age")
LOGIN_MODES = ("login", "setup-token")


def cred_status_word(st):
    """Статус кредита по-русски для страницы: три исхода плюс «не проверялся».
    Правило одно на CLI и страницу (#317, credreg.status_word), таблица слов --
    здесь.

    Время сброса квоты в слово не входит (#331): здесь оно было бы в поясе
    сервера и без даты, и недельное окно через три дня читалось как «сегодня
    в пять утра». resets_at едет в строке числом, и «до …» пишет страница в
    поясе браузера."""
    return credrows.status_word(st, CRED_WORDS, "не проверялся")


def cred_rows(records, now, holders=None):
    """Записи реестра -> строки страницы: имя, профиль, вид, владелец, статус
    словами, время сброса, процент худшего окна, возраст и держатели аренды.
    Ключ, токен и прочее содержимое записи сюда не переписываются.

    holders -- {кредит: {папет: узел}} из credreg.holders() (#301): та же
    аренда, что у `mop cred list`. Нет карты -- у каждой строки пустой
    список, а не отсутствие ключа: странице не надо гадать."""
    holders = holders or {}
    out = []
    for rec in records:
        st = rec.get("status") or {}
        name = rec.get("name") or "-"
        out.append({"name": name, "profile": rec.get("profile") or "-",
                    "kind": rec.get("kind") or "-", "owner": rec.get("owner") or "",
                    "status": cred_status_word(st), "resets_at": st.get("resets_at"),
                    "percent": st.get("percent"),
                    "age": credrows.age(rec, now),
                    "holders": sorted(holders.get(name) or {})})
    return out


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


def parse_login_start(body):
    """Тело /api/creds/login/start -> ({name, mode}, None) либо (None, причина)."""
    body, err = _body(body)
    if err:
        return None, err
    name, err = _field(body, "name")
    if err:
        return None, err
    try:
        credrows.check_name(name)
    except ValueError as e:
        return None, f"name: {e}"
    # Без режима -- None: режим решает вид кредита в login_start (#339).
    mode, _ = _field(body, "mode", required=False)
    mode = mode or None
    if mode is not None and mode not in LOGIN_MODES:
        return None, f"mode: one of {', '.join(LOGIN_MODES)}"
    return {"name": name, "mode": mode}, None


def parse_login_code(body):
    """Тело /api/creds/login/code -> ({name, code}, None) либо (None, причина)."""
    body, err = _body(body)
    if err:
        return None, err
    name, err = _field(body, "name")
    if err:
        return None, err
    code, err = _field(body, "code")
    if err:
        return None, err
    return {"name": name, "code": code}, None


# ─── сборщик ─────────────────────────────────────────────────────────────
class Collector:
    """Держит снимок и пять потоков, которые его обновляют: states, sizes,
    usage, creds, masters (start).

    Версия растёт на каждом изменении; `wait` отдаёт SSE-клиенту новую
    версию или таймаут. Ошибки кругов лежат в снимке по имени круга, а не
    роняют сборщик: легла шина — ростер из Nomad всё равно показывается,
    ровно как в `mop list`."""

    def __init__(self):
        self._cond = threading.Condition()
        self.version = 0
        self.rows, self.sizes, self.nodes = [], {}, []
        self.usage, self.per_puppet, self.per_user, self.journal = [], [], [], []
        self.creds = []
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
                            self.at, self.creds, self.masters)

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
        for fn in (self._states, self._sizes, self._usage, self._creds, self._masters):
            threading.Thread(target=fn, daemon=True, name=f"mop-web-{fn.__name__}").start()

    def _note(self, kind, error):
        if error is None:
            self.errors.pop(kind, None)
        else:
            self.errors[kind] = error

    def _states(self):
        while True:
            try:
                rows = puppets.puppet_rows(sizes=False)
                # Страница читает ключи строки nodes по имени: наружу --
                # прежняя форма провода (#267).
                nodes = [n.to_row() for n in puppets.nodes()]
                with self._cond:
                    self.rows, self.nodes = rows, nodes
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
                per_day, per_puppet, per_user = gather_usage()
                with self._cond:
                    self.usage = usage_axis(per_day, USAGE_DAYS)
                    self.per_puppet = per_puppet
                    self.per_user = per_user
                    self._note("usage", None)
            except Exception as e:
                with self._cond:
                    self._note("usage", str(e) or type(e).__name__)
            self._bump()
            time.sleep(USAGE_EVERY)


    def _creds(self):
        # Реестр лежит на сервере рядом с сервисом, и читается напрямую
        # (mop/server/credreg.py), а не глаголом кластера. Ростер и узлы
        # сборщик спрашивает у сервиса кластера по субъекту без логина --
        # он service открыт намеренно; с логином -- закрыт (#309, docs/BUS.md).
        while True:
            self.refresh_creds()
            time.sleep(CREDS_EVERY)

    def _masters(self):
        # Живые мастера (#305): опрос who в общий инбокс каждого проекта --
        # тот же, которым их находит инструмент agents. Проекты -- реестр
        # сервера (и проекты без папетов) плюс проекты ростера. service
        # публиковать туда вправе (mop.> без rpc), ответы -- в _INBOX.
        from ..common import busnames, projects as registry
        while True:
            try:
                with self._cond:
                    rows = list(self.rows)
                names = set(registry.names(*puppets.project_ids(registry.read())))
                names |= {puppets.project_of(r.origin) for r in rows if r.origin}
                answers = {p: bus.gather("who", timeout=MASTERS_WAIT,
                                         subj=busnames.inbox(p, busnames.ALL_MASTERS))
                           for p in sorted(names)}
                found = master_rows(answers, rows)
                with self._cond:
                    self.masters = found
                    self._note("masters", None)
            except Exception as e:
                with self._cond:
                    self._note("masters", str(e) or type(e).__name__)
            self._bump()
            time.sleep(MASTERS_EVERY)

    def refresh_creds(self):
        """Перечитать реестр сейчас: после добавления или логина со страницы
        строка обязана появиться без минуты ожидания."""
        try:
            from . import credreg
            credreg.login_forget_expired()
            # Аренда -- из меты джобов, тем же токеном Nomad пользователя
            # пула, что у сервиса кластера. Не прочиталась -- строки без
            # держателей, реестр всё равно показываем.
            try:
                held = credreg.holders()
            except Exception:
                held = {}
            rows = cred_rows(credreg.all(), time.time(), held)
            with self._cond:
                self.creds = rows
                self._note("creds", None)
        except Exception as e:
            with self._cond:
                self._note("creds", str(e) or type(e).__name__)
        self._bump()


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


