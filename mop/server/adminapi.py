"""Тонкий admin HTTP API поверх общих операций и исполнителя (#461, #499).

Серверный адаптер, и только он: гейты, разбор узкой схемы, реестр
операций, приём и статусы. Workflow здесь не исполняется -- ни в потоке
запроса, ни где-либо: POST принимает операцию (202), исполнение --
поток на операцию, результат читается отдельным GET'ом.

Приём (#499) -- словарь в памяти: тот же payload -- тот же id (двойной
клик не рождает вторую операцию), другой payload на ту же цель --
вторая операция: исполнитель ждёт семафор цели (страница видит
waiting), а не отказывает 409 (#497: отказ висел часами). Завершённая
операция освобождает ключ и цель: явный повтор -- новая операция,
дедуп -- про живой клик, не про историю. Рестарт службы стирает память:
незнакомый id читается unknown со ссылкой на журнал -- дюрабельности
«клик -> вердикт» через рестарт нет, правда об эффекте -- аудит пула
(ops.record) и /api/journal, как у CLI.

Сериализация -- семафор на устойчивую идентичность цели (effective_
target): операции одной цели едут по очереди, разных -- параллельно.
Семафор адаптера держится до ОТВЕТА операции (терминал), а полосу цели
держит воркер (#569): пережившая срок нитка эффекта остаётся держателем
до фактического конца, и следующая операция ждёт её там (drain), а не
здесь.

Вердикт (#499) публикуется терминальным SSE-кадром operation в
существующий поток /events (#424) -- через on_terminal-крючок адаптера;
кадр несёт {id, action, target, status, error?} и не несет payload.

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

Коды различимы: 202 -- только принятие (ничего не исполнено), 4xx --
аутентификация и валидация, 503 -- транспорт. Детали ошибок --
allowlist-фразы (sanitize), не raw exceptions: в них ходят пути и секреты.

Send-to-puppet первого выпуска: только адресная доставка папету, вердикт
succeeded/refused/unknown; без notify_when_idle, wait-until-done и
двустороннего веб-чата. Текст сообщения живёт только в живой операции
памяти -- не в ответе, не в результате, не в URL, не в SSE-кадре.

Реестр операций общий с библиотекой (#458 -- сами операции, #460 --
safety); HTTP-whitelist может быть у́же CLI, но не копией алгоритма.
Незарегистрированная операция -- unsupported (404): кнопка в GUI не
показывается, пока операция не подключена. GET результатов -- admin-only
и no-store, в публичный SSE payload результата не попадает; обновление
публичного ростера не подтверждает операцию.

Исполнитель (#499) подключается фабрикой эффектов: executor(action,
target, params, who) -> эффект, адаптер зовёт его в потоке операции.
До подключения endpoint отвечает 503 -- интерфейс честен.

GET /api/admin/projects (#472) -- список заведённых проектов для формы
add: реестр сервера, admin-only, чтение без побочных действий (Origin-гейт
-- защита записи, чтению он не нужен, как и GET результата). Ответ --
имя и origin: имя выводится project_of origin'а, легаси-имена без
origin'а форме не участвуют -- обратного отображения «имя -> origin»
в системе нет (#33). no-store ставит обвязка HTTP.

GET /api/admin/models (#468) -- каталог моделей прокси для формы смены:
админ-only так же, ответ {"models": [id]}. Источник читается сервером
(mop/server/adminapi.catalog_read: петля 127.0.0.1:MOP_LLM_PROXY_PORT,
ключ из файла установленного прокси, #391, #557) -- ключ и URL в ответ
не едут; парсер общий с клиентом (#448, mop/common/llm.catalog_ids),
второй копии urllib-логики здесь нет. Лежащий источник -- closed 503
фразой allowlist'а.
"""
import json
import re
import sys
import threading
import time
import uuid

from ..common import config, puppets
from . import opworker
from .opworker import TERMINAL, status_of

# ─── лимиты ──────────────────────────────────────────────────────────────
BODY_MAX = 64 * 1024        # запросы -- килобайты: параметры, не файлы

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


def result_error(status, error):
    """Публичная причина — закрытая фраза, подробности остаются в журнале.

    Фильтрации пунктуации недостаточно: ключ или текст сообщения могут
    состоять из обычных букв. Ни один фрагмент сырой ошибки не копируется.
    """
    fallback = {
        "refused": "operation refused",
        "failed": "operation failed",
        "partial": "operation completed only partially",
        "unknown": "operation outcome is unknown; inspect the target before retrying",
    }.get(status)
    if fallback is None:
        return None
    text = str(error or "")
    reasons = (
        ("work in the clone", "target has unfinished work"),
        ("led by", "target is owned by another user"),
        ("held by", "target is owned by another user"),
        ("cannot inspect the target", "target state is unavailable"),
        ("cannot tell if it holds work", "target state is unavailable"),
        ("cannot determine the target project", "target project is unavailable"),
        ("no live session", "target has no live session"),
        ("not running", "target is not running"),
        ("send needs only a non-empty message", "send needs a non-empty message"),
        ("message delivery could not be confirmed", "message delivery could not be confirmed"),
    )
    return next((public for fragment, public in reasons if fragment in text), fallback)


class Refused(Exception):
    """Валидационный отказ адаптера: (код, фраза)."""

    def __init__(self, code, error):
        super().__init__(error)
        self.code, self.error = code, error


def catalog_read(env=None, key_path=None):
    """Каталог моделей прокси глазами сервера (#468, #557): GET /v1/models
    клиентским ключом. Ключ -- файл установленного прокси (proxykey.source,
    #391), прежний env -- переходный. База -- петля llm.internal_loop
    (http://127.0.0.1:MOP_LLM_PROXY_PORT): читатель стоит на самом
    контроллере рядом с прокси, публичный origin через nginx и пин CA
    ему не нужны -- вторая копия TLS-правила #517 разъехалась бы с
    первой. Ключ не логируется и в ответы не едет -- только внутрь
    HTTP-запроса. -> [id] | исключение (лежащий прокси, нет ключа):
    фасад переведёт в общую фразу 503. Сервер-слой: proxykey общему
    слою не виден (imports go downward only)."""
    import urllib.request
    from . import proxykey
    from ..common import llm
    key = proxykey.source(env or {}, key_path)
    if not key:
        raise RuntimeError("proxy key is not configured")
    req = urllib.request.Request(llm.endpoint(llm.internal_loop(), "models"),
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        if r.status != 200:
            raise RuntimeError(f"proxy catalog answered HTTP {r.status}")
        return llm.catalog_ids(json.load(r))


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
    """Адаптер: handler -> ответ. Гейты в порядке цены ошибки, приём и
    статусы -- в памяти (#499), исполнение -- поток на операцию."""

    def __init__(self, operations, origin, who=None, provider=None,
                 now=time.time, executor=None):
        """who -- личность запроса (login, role) из сессии handler'а, не
        конструктора: каждый запрос приходит со своей. executor --
        фабрика эффекта (action, target, params, who) -> эффект:
        адаптер зовёт её в потоке операции, эффект возвращает вердикт
        воркера ({status, error, ...}, статусы #476)."""
        self.ops = operations
        self.origin = origin
        self.who = who
        self._provider = provider
        self._now = now
        self.executor = executor
        self._lock = threading.Lock()
        # id -> запись результата; payload в записи НЕ живёт.
        self._records = {}
        # ключ payload -> id: пока операция жива, повтор -- тот же id.
        self._by_key = {}
        # цель -> семафор: одна операция на цель, вторая ждёт (#497).
        self._gates = {}
        self._on_terminal = []
        # Внутренний аудит завершения (#575): отдельная проекция от
        # публичного кадра /events.
        self._on_completion = []
        # Контексты аудита по id операций (#575): заводит исполнитель
        # веб-слоя ДО старта эффекта, забирает _execute по терминалу.
        self._audit_context = {}
        # id операции, исполняемой текущим потоком (#575): фабрика
        # эффектов узнаёт его без смены сигнатуры.
        self._current = threading.local()

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
        """Гейты + разбор + приём. Ответ -- через handler._json."""
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

    # ─── приём и повторы ───────────────────────────────────────────────
    @staticmethod
    def effective_target(action, target, params):
        """Устойчивая идентичность операции (#470) и полосы цели (#569):
        target, а для операции без цели -- origin из params. Правило
        живёт в opworker.effective_target -- одной точке для дедуп-ключа
        адаптера и полосы воркера: два слоя не могут разойтись. Две
        подачи одного origin -- один спор, подачи разных origin'ов друг
        другу не мешают."""
        return opworker.effective_target(action, target or None, params)

    def submit(self, action, target, params, who):
        """Операция -> поток исполнения + запись результата. Повтор того же
        payload, пока операция жива, -- тот же id; другой payload на ту же
        цель -- вторая операция: она ждёт семафор цели, а не отказывает
        (#497). Завершённая операция ключ освобождает: явный повтор --
        новая операция."""
        params = params or {}
        login = (who or (None,))[0]
        key = (action, self.effective_target(action, target, params),
               tuple(sorted(params.items())))
        with self._lock:
            known = self._by_key.get(key)
            if known is not None:
                rec = self._records[known]
                return {k: rec[k] for k in ("id", "action", "target",
                                            "status", "login", "at")}
            if self.executor is None:
                raise Refused(503, "operations executor is not wired")
            op_id = uuid.uuid4().hex
            record = {"id": op_id, "action": action, "target": target,
                      "status": "accepted", "login": login, "at": self._now()}
            self._records[op_id] = record
            self._by_key[key] = op_id
        threading.Thread(target=self._execute,
                         args=(record, key, params, who),
                         daemon=True, name=f"mop-web-op-{op_id[:8]}").start()
        return dict(record)

    def _gate(self, target):
        """Семафор цели: одна операция на цель (#499)."""
        with self._lock:
            return self._gates.setdefault(target, threading.Semaphore(1))

    def _mark(self, record, status):
        with self._lock:
            record["status"] = status

    def _execute(self, record, key, params, who):
        """Поток операции (#499): waiting -> семафор цели -> running ->
        эффект -> терминал. Семафор адаптера отпирается по ОТВЕТУ
        операции (терминал); полосу цели после ответа держит воркер
        (#569): пережившая срок нитка эффекта остаётся держателем до
        фактического конца, и следующая операция той же цели ждёт её
        там (drain), не здесь -- иначе мутации двух операций гонялись бы
        по одной цели вперегонки."""
        gate = self._gate(self.effective_target(record["action"],
                                                record["target"], params))
        self._mark(record, "waiting")
        gate.acquire()
        try:
            self._mark(record, "running")
            # Контекст потока -- ДО фабрики эффектов (#575): фабрика
            # читает current_op() и регистрирует контекст аудита до
            # старта эффекта; сигнатура фабрики не меняется.
            self._current.op_id = record["id"]
            effect = self.executor(record["action"], record["target"],
                                   params, who)
            verdict = effect() if callable(effect) else effect
            status = status_of(verdict)
        except Exception as e:  # noqa: BLE001 -- вердикт с причиной, не тишь
            status, verdict = "failed", {"error": str(e)}
        finally:
            gate.release()
        with self._lock:
            rec = self._records.get(record["id"])
            if rec is not None:
                rec["status"] = status
                if isinstance(verdict, dict) and verdict.get("error"):
                    rec["error"] = verdict["error"]
                if self._by_key.get(key) == record["id"]:
                    self._by_key.pop(key, None)
        # Одно внутреннее completion-событие (#575): запись приёма +
        # подтверждённый контекст эффекта -- источник обеих проекций.
        # Контекст -- заводит исполнитель веб-слоя (audit-словарь на
        # операцию): имя цели из записи приёма (existing-target), проект
        # -- факт в момент подтверждения; вердикт доопределяет пустое
        # (add называет созданного). Никаких сетевых lookup после
        # эффекта: не подтвердился -- честный None.
        ctx = self._audit_context.pop(record["id"], None) or {}
        name = ctx.get("name") or record["target"]
        audit = {"id": record["id"], "action": record["action"],
                 "status": status, "login": record["login"],
                 "name": name if isinstance(name, str) and name else None,
                 "project": ctx.get("project")}
        # Публичный кадр (/events) -- ПРОЕКЦИЯ completion: статус,
        # закрытая фраза, без payload; login и проект в него не идут.
        frame = {"id": audit["id"], "action": audit["action"],
                 "target": record["target"], "status": audit["status"]}
        reason = result_error(status, rec.get("error") if rec else None)
        if reason:
            frame["error"] = reason
        # Сырая причина неуспеха -- в журнал службы (#506): после #499
        # (журнал ops.jsonl удалён) текст исключения жил только в памяти
        # процесса, и живой отказ читался «operation failed» без улик.
        # Одна строка в stderr (юнит не переопределяет StandardError --
        # она в journalctl -u mop-web); успех молчит, публичный кадр и
        # GET остаются на закрытой фразе (#495), ничего не едет на шину.
        if status in TERMINAL and status != "succeeded":
            raw = (rec or {}).get("error") if isinstance(rec, dict) else None
            if isinstance(verdict, dict) and verdict.get("error"):
                raw = verdict["error"]
            print(f"op {record['id']} {record['action']} "
                  f"{record['target']} login={record['login']} "
                  f"{status}: {raw or 'no reason captured'}",
                  file=sys.stderr, flush=True)
        for cb in list(self._on_terminal):
            cb(frame)
        for cb in list(self._on_completion):
            cb(audit)

    def on_terminal(self, callback):
        """Крючок терминала (#499): кадр {id, action, target, status,
        error?} -- в SSE /events (#424) и в журнал службы. Payload в кадре
        нет; причина -- закрытая фраза result_error, не сырой error."""
        self._on_terminal.append(callback)

    def on_completion(self, callback):
        """Крючок внутреннего аудита завершения (#575): событие {id,
        action, status, login, name?, project?} -- источник ops.record
        на шину. Публичный кадр /events шире не становится: login и
        project в него не попадают; сюда они доходят из записей
        адаптера, а не из payload запроса."""
        self._on_completion.append(callback)

    def current_op(self):
        """id операции, исполняемой ЭТИМ потоком (#575), | None.

        Фабрика эффектов зовёт его в потоке операции и регистрирует
        контекст аудита до старта эффекта."""
        return getattr(self._current, "op_id", None)

    def audit_context(self, op_id, context):
        """Завести контекст аудита операции (#575): {name?, project?} --
        факты подтверждённого контекста эффекта. Зовёт исполнитель
        веб-слоя ДО старта эффекта; _execute забирает словарь по
        терминалу. Контекст одного op не смешивается с соседним."""
        with self._lock:
            self._audit_context[op_id] = context

    # ─── GET результата ────────────────────────────────────────────────
    def result(self, op_id, who):
        """Результат по id. Admin-only; ответ -- без payload. Незнакомый id
        (рестарт службы, чужой) -- unknown со ссылкой на журнал: правда об
        эффекте -- /api/journal и аудит пула, не память адаптера."""
        why = self._admin(who, asked=True)
        if why:
            raise Refused(why[0], why[1])
        with self._lock:
            stored = self._records.get(op_id)
            got = dict(stored) if stored is not None else None
        if got is None:
            return {"status": "unknown",
                    "error": "operation outcome is unknown; "
                             "inspect the pool journal (/api/journal)"}
        # Публичного в результате нет: ни params, ни текста сообщения.
        result = {k: v for k, v in got.items() if k in ("id", "action", "target",
                                                       "status", "login", "at")}
        reason = result_error(got["status"], got.get("error"))
        if reason:
            result["error"] = reason
        return result

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

    # ─── GET /api/admin/models ──────────────────────────────────────────
    def models(self, handler, read=None):
        """Каталог моделей прокси (#468): id из /v1/models. Admin-only,
        чтение без побочных действий; read -- читатель сервера (подмена
        проверками; по умолчанию llm.catalog_read). Ответ -- {"models":
        [id]}: ни ключа, ни URL прокси в нём нет."""
        why = self._admin(getattr(handler, "who", None), asked=True)
        if why:
            return handler._json(why[0], {"error": why[1]})
        if read is None:
            read = catalog_read
        try:
            got = read() or []
        except Exception:  # noqa: BLE001 -- наружу общая фраза: в сыром
            # исключении ходят адреса и пути, а диагноз один -- источника
            # нет; какой именно, админу формы знать не нужно.
            return handler._json(503, {"error": "model catalog is unavailable"})
        return handler._json(200, {"models": sorted(got)})
