"""Тонкий admin HTTP API поверх общих операций и исполнителя (#461).

Серверный адаптер, и только он: гейты, разбор узкой схемы, реестр
операций, постановка воркеру (#476), ответы. Workflow здесь не исполняется
-- ни в потоке запроса, ни где-либо: операция уезжает задачей, результат
читается отдельным GET'ом.

Гейты, по убыванию цены ошибки:
    роль       admin на каждом POST и GET результата: гость -- 401,
               вошедший не-admin -- 403. Роль перечитывается провайдером
               на каждом запросе (как /api/journal, #424), провайдер лёг --
               закрыто, без сырой ошибки наружу.
    Origin     POST без Origin или с чужим -- 403: браузер обязан слать,
               отсутствие -- не браузер (#415).
    тело       Content-Type строго application/json, Content-Length
               ограничена и прочитана целиком, тело -- JSON-объект без
               неизвестных полей.

Схема узкая: {action, target, params}. Никаких произвольных RPC
subject/verb, shell/argv/cwd/env, адресов удалённых серверов: такие поля --
«неизвестные» и режутся на разборе, а не фильтруются потом.

Коды различимы: 202 -- только принятие (ничего не исполнено), 409 --
конфликт (payload/цель изменились, конкурирующая операция в очереди),
4xx -- аутентификация и валидация, 503 -- транспорт. Детали ошибок --
allowlist-фразы (sanitize), не raw exceptions: в них ходят пути и секреты.

Send-to-puppet первого выпуска: только адресная доставка папету, вердикт
accepted/delivered/held/refused/unknown; без notify_when_idle,
wait-until-done и двустороннего веб-чата. Текст сообщения живёт только в
приватной задаче очереди с ограниченным сроком -- не в ответе, не в
результате, не в URL, не в публичном снимке.

Реестр операций общий с библиотекой (#458 -- сами операции, #460 --
safety); HTTP-whitelist может быть у́же CLI, но не копией алгоритма.
Незарегистрированная операция -- unsupported (404): кнопка в GUI не
показывается, пока операция не подключена. GET результатов -- admin-only
и no-store, в публичный SSE не попадают; обновление публичного ростера не
подтверждает операцию.

Воркер (#476) подключается регистрацией исполнителя: Operations.register
добавляет действие и его параметры. До этого endpoint отвечает
unsupported -- интерфейс честен.

GET /api/admin/projects (#472) -- список заведённых проектов для формы
add: реестр сервера, admin-only, чтение без побочных действий (Origin-гейт
-- защита записи, чтению он не нужен, как и GET результата). Ответ --
имя и origin: имя выводится project_of origin'а, легаси-имена без
origin'а форме не участвуют -- обратного отображения «имя -> origin»
в системе нет (#33). no-store ставит обвязка HTTP.
"""
import json
import re
import threading
import time
import uuid

from ..common import puppets

# ─── лимиты и сроки ──────────────────────────────────────────────────────
BODY_MAX = 64 * 1024        # запросы -- килобайты: параметры, не файлы
RECORD_TTL = 24 * 3600      # результаты живут сутки, payload в очереди -- столько же
QUEUE_MAX = 64              # очередь не бесконечна: заполненная -- 503

# Что разрешено первым выпуском (send): адресная доставка папету.
SEND_PARAMS = ("message",)
# Какие параметры принимает операция: задаётся регистрацией.

_WORD = re.compile(r"[A-Za-zА-Яа-я0-9._:'\" -]+")
# Слэшей и расширений (.key, .env) в фразе не остаётся: путь не предмет
# фразы. Дефис внутри слова живёт: pu-mop-1 -- имя.
# Разрешённые поля запроса: всё прочее -- неизвестное, отказ.
KNOWN_FIELDS = ("action", "target", "params")


def sanitize(error):
    """Ошибка -> allowlist-фраза: буквы, цифры, минимум пунктуации. Путь,
    ключ и стек в ней не выживают; пусто -- «error»."""
    # Путь -- не фраза: слэш-цепочка уходит целиком, до разбора слов.
    stripped = re.sub(r"[/\\]\S*", " ", str(error))
    words = " ".join(_WORD.findall(stripped)).strip()
    # Пунктуацию сведём к пробелам, кроме дефиса внутри слова: имя папета
    # остаётся именем, а путь и расширение -- исчезают.
    squeezed = re.sub(r"[._:'\"()]+", " ", words)
    squeezed = re.sub(r"\s+", " ", squeezed).strip()
    return squeezed or "error"


class Refused(Exception):
    """Валидационный отказ адаптера: (код, фраза)."""

    def __init__(self, code, error):
        super().__init__(error)
        self.code, self.error = code, error


def project_list(known):
    """Строки реестра -> [{name, origin}] для формы add (#472).

    Имя -- project_of origin'а, единственное определение проекта (#154).
    Легаси-строки (голые имена без origin-вида) выпадают: форму add кормит
    origin, а не имя, обратного отображения нет (#33). Отсортировано --
    список в ответе не зависит от порядка множества."""
    from .. import driver  # как в cluster.py: server импортирует driver
    if callable(known):     # провайдер-читатель реестра, как в projects()
        known = known()
    out = []
    for line in known:
        if not puppets.looks_like_origin(line):
            continue
        out.append({"name": driver.project_of(line), "origin": line})
    return sorted(out, key=lambda p: p["name"])


class Queue:
    """Очередь задач воркеру (#476). Задача -- словарь; текст сообщения --
    только здесь, приватно и с сроком."""

    def __init__(self, maxsize=QUEUE_MAX):
        self._items = []
        self._lock = threading.Lock()
        self._maxsize = maxsize
        self.put = self._put

    def _put(self, job):
        with self._lock:
            self._sweep()
            if len(self._items) >= self._maxsize:
                raise Refused(503, "operation queue is full")
            self._items.append(job)

    def take(self):
        with self._lock:
            self._sweep()
            return self._items.pop(0) if self._items else None

    def _sweep(self):
        now = time.time()
        self._items = [j for j in self._items if j.get("_expires", now + 1) > now]


class Operations:
    """Реестр допустимых операций и их параметров. Регистрация -- точка
    стыковки с воркером (#476) и операциями (#458): пока действие не
    зарегистрировано, endpoint отвечает unsupported."""

    def __init__(self):
        self._lock = threading.Lock()
        self._actions = {"send": SEND_PARAMS}
        # Операции без цели (#470): add заводит нового папета -- имени ещё
        # нет, его присвоит сервис, и цель клика -- сам origin.
        self._targetless = set()

    def register(self, action, params, target=True):
        with self._lock:
            self._actions[action] = tuple(params)
            if not target:
                self._targetless.add(action)
            else:
                self._targetless.discard(action)

    def needs_target(self, action):
        with self._lock:
            return action not in self._targetless

    def has(self, action):
        with self._lock:
            return action in self._actions

    def params(self, action):
        with self._lock:
            return self._actions.get(action, ())

    def check(self, action, params):
        """Валидация params операции: только объявленные ключи."""
        if not self.has(action):
            raise Refused(404, "unsupported operation")
        allowed = self.params(action)
        unknown = sorted(set(params or {}) - set(allowed))
        if unknown:
            raise Refused(400, "unknown parameters: " + ", ".join(unknown))


class Api:
    """Адаптер: handler -> ответ. Гейты в порядке цены ошибки."""

    def __init__(self, operations, queue, origin, who=None,
                 provider=None, now=time.time):
        """who -- личность запроса (login, role) из сессии handler'а, не
        конструктора: каждый запрос приходит со своей."""
        self.ops = operations
        self.queue = queue
        self.origin = origin
        self.who = who
        self._provider = provider
        self._now = now
        self._lock = threading.Lock()
        # id -> запись результата; payload в записи НЕ живёт.
        self._results = {}
        # (action, target) -> id активной задачи: повтор и конфликт.
        self._active = {}

    # ─── роль: перечитывается на каждом запросе ────────────────────────
    def _admin(self, who=None, asked=False):
        """(login, role) | отказ. Провайдер лёг -- закрыто, без сырых
        деталей: в исключении провайдера ходят пути и креды. who -- личность
        ЗАПРОСА (None -- гость), asked отличает «не спросили» от «гость»."""
        pair = who if asked or who is not None else self.who
        if self._provider is None:
            login, role = pair or (None, None)
        else:
            try:
                login, role = self._provider() or (None, None)
            except Exception:
                return 503, "login provider unavailable"
        if not login:
            return 401, "login required"
        if role != "admin":
            return 403, "admin only"
        return None

    # ─── POST /api/admin/ops ───────────────────────────────────────────
    def post(self, handler):
        """Гейты + разбор + постановка. Ответ -- через handler._json."""
        why = self._admin(getattr(handler, "who", None), asked=True)
        if why:
            return handler._json(why[0], {"error": why[1]})
        try:
            action, target, params = self._parse(handler)
            record = self.submit(action, target, params,
                                 getattr(handler, 'who', None) or self.who)
        except Refused as e:
            return handler._json(e.code, {"error": e.error})
        except Exception as e:  # noqa: BLE001 -- наружу только фраза
            return handler._json(500, {"error": sanitize(e)})
        # 202 -- только принятие: ничего не исполнено.
        return handler._json(202, {"id": record["id"], "status": "accepted"})

    def _parse(self, handler):
        """Handler -> (action, target, params). Всё лишнее -- Refused."""
        if handler.command != "POST":
            raise Refused(405, "POST only")
        if handler.headers.get("Origin") != self.origin:
            raise Refused(403, "origin refused")
        ctype = (handler.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype != "application/json":
            raise Refused(415, "application/json required")
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            raise Refused(400, "bad content length")
        if length < 0 or length > BODY_MAX:
            raise Refused(413, "request too large")
        body = handler.rfile.read(length) if length else b""
        if len(body) != length:
            raise Refused(400, "incomplete request")
        try:
            got = json.loads(body or b"{}")
        except ValueError:
            raise Refused(400, "invalid JSON")
        if not isinstance(got, dict):
            raise Refused(400, "JSON object expected")
        unknown = sorted(set(got) - set(KNOWN_FIELDS))
        if unknown:
            # Произвольный subject/verb/shell/actor/env -- тот же отказ.
            raise Refused(400, "unknown fields: " + ", ".join(unknown))
        action, target, params = got.get("action"), got.get("target"), got.get("params") or {}
        if not isinstance(action, str) or not isinstance(params, dict):
            raise Refused(400, "schema: {action, target, params}")
        # Цель обязательна, кроме зарегистрированных без неё (#470): add
        # кладёт origin в params, и присвоенное имя едет фактом записи.
        if target is None and self.ops.needs_target(action) or \
                (target is not None and not isinstance(target, str)):
            raise Refused(400, "schema: {action, target, params}")
        self.ops.check(action, params)
        return action, target, params

    # ─── постановка и повторы ──────────────────────────────────────────
    @staticmethod
    def effective_target(action, target, params):
        """Устойчивая идентичность операции (#470): target, а для операции
        без цели -- origin из params. Две подачи одного origin -- один
        спор, подачи разных origin'ов друг другу не мешают."""
        if target is None:
            return params.get("origin")
        return target

    def submit(self, action, target, params, who):
        """Операция -> задача в очереди + запись результата. Повтор того
        же payload -- тот же id; другой payload на ту же цель -- конфликт,
        пока прежняя задача в очереди."""
        key = (action, self.effective_target(action, target, params),
               tuple(sorted(params.items())))
        with self._lock:
            self._sweep()
            prior = self._active.get((action,
                                      self.effective_target(action, target,
                                                            params)))
            if prior and prior.get("key") == key:
                # Идемпотентный повтор: тот же ответ без второй задачи.
                return self._results[prior["id"]]
            if prior:
                raise Refused(409, "another operation is queued for this target")
            op_id = uuid.uuid4().hex
            record = {"id": op_id, "action": action, "target": target,
                      "status": "accepted", "login": (who or (None,))[0],
                      "at": self._now()}
            self._results[op_id] = record
            self._active[(action, target)] = {"id": op_id, "key": key}
        # Payload -- только в задаче, с сроком; запись результата его не несёт.
        self.queue.put({"id": op_id, "action": action, "target": target,
                        "params": params, "login": record["login"],
                        "_expires": self._now() + RECORD_TTL})
        return record

    # ─── GET результата ────────────────────────────────────────────────
    def result(self, op_id, who):
        """Результат по id. Admin-only; ответ -- без payload."""
        why = self._admin(who, asked=True)
        if why:
            return {"status": str(why[0])}
        with self._lock:
            self._sweep()
            got = self._results.get(op_id)
        if got is None:
            return {"status": "not found"}
        # Публичного в результате нет: ни params, ни текста сообщения.
        return {k: v for k, v in got.items() if k in ("id", "action", "target",
                                                      "status", "login", "at")}

    # ─── GET /api/admin/projects ────────────────────────────────────────
    def projects(self, handler, known):
        """Список заведённых проектов (#472): имя и origin из реестра.
        Admin-only, чтение без побочных действий; реестр живёт на сервере
        (#117), форма add берёт его отсюда, а не с машины админа."""
        why = self._admin(getattr(handler, "who", None), asked=True)
        if why:
            return handler._json(why[0], {"error": why[1]})
        try:
            listing = project_list(known)
        except Exception as e:  # noqa: BLE001 -- наружу только фраза
            return handler._json(503, {"error": sanitize(e)})
        return handler._json(200, {"projects": listing})

    def _sweep(self):
        now = self._now()
        stale = [i for i, r in self._results.items() if now - r["at"] > RECORD_TTL]
        for i in stale:
            self._results.pop(i, None)
        for key, act in list(self._active.items()):
            if act["id"] in stale:
                self._active.pop(key, None)
