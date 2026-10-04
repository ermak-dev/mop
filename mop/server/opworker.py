"""Исполнитель веб-операций: устойчивый результат и восстановление (#476).

Дашборд действует (карточка папета #462, кнопки #461): клик человека
должен выполняться библиотечным workflow #458 под инвариантом #460 и
переживать крах службы с честным исходом. Воркер -- не фоновый subprocess
командлета и не привилегированный RPC: тонкий слой приёма, устойчивая
запись и вызов готовых функций.

Устойчивость -- каталог в ~/.config/mop сервера (приватный, 0700 у
каталога пула): журнал операций append-only (ops.jsonl), payload
исполнителя -- отдельным файлом на операцию (payload/<id>.json) со своим
сроком, из ответов GET его нет. Хранилище недоступно -- операция не
запускается вовсе: тихо продолжить без записи -- потерять результат.

Приём транзакционен: intent (actor, ключ, хэш нормализованного запроса)
пишется ДО эффекта; тот же ключ тем же actor с тем же хэшем -- та же
операция (двойной клик), с другим хэшем -- отказ. Роль проверяется до
первого эффекта: понижение отменяет ещё не начатое; начатое доводится
до этапа и аудируется.

Перезапуск службы: незавершённая running-запись помечается unknown и
НЕТ реконсиляции -- ничего не переигрывается и не сверяется с
job/alloc/session; unknown запрещает автоматический повтор (это не
failed), решение -- за админом по карточке папета.

Конфликтующие действия сериализуются по устойчивой идентичности цели
(имя папета): вторая операция над той же целью ждёт в очереди.

Никаких unbounded потоков: исполнение -- в вызывающем потоке (HTTP-сервер
своим тредом на запрос), никакого состояния в браузере.
"""
import hashlib
import json
import os
import threading
import time

from ..common import bus, ops, puppets

# Сроки и лимиты (#476): настраиваются атрибутами воркера, значения --
# заведённые константы установки.
RETENTION_DAYS = 30          # сколько жить завершённым записям
PAYLOAD_DAYS = 3             # payload исполнителя живёт короче записей
QUEUE_LIMIT = 100            # незавершённых операций в очереди на приём
OBSERVE_WAIT = 60            # сколько наблюдать рестарт (#464)
OBSERVE_POLL = 2             # шаг опроса наблюдения

# Модель (#476): точные переходы. unknown -- не failed: эффекта могли и
# быть, и не быть; повтор автоматом запрещён.
STATUSES = ("queued", "running", "succeeded", "refused", "failed",
            "partial", "unknown")
TERMINAL = ("succeeded", "refused", "failed", "partial", "unknown")

# Карта исхода workflow #458 в статус операции: registered -- эффект
# сошёлся, accepted -- эффект без наблюдения (partial), refused -- отказ,
# unknown/failed -- ни эффекта, ни отказа.
_FROM_OUTCOME = {"registered": "succeeded", "accepted": "partial",
                 "refused": "refused", "failed": "failed",
                 "partial": "partial",
                 "unknown": "unknown"}

# Параметры веб-операций (#470): реестр HTTP-whitelist'а (#461) и связь
# цель/запрос у адаптера -- из одной точки. add без цели: имя присвоит
# сервис, target клика -- сам origin; add_request/update_request --
# валидаторы params, их отказ -- отказ приёма, до intent.
ADD_PARAMS = ("origin", "branch")
UPDATE_PARAMS = ("fresh", "origin")
# Разрушительные действия (#471) аргументов не имеют: force веб не берёт
# по определению (#473), пустой кортеж -- «никаких лишних полей».
RUIN_PARAMS = ()
# Смена модели (#468): один параметр -- id из каталога прокси. Форму
# проверяет ops.valid_model_id (слэш/перевод строки/управление там не
# проходят); «прокси такое не знает» -- внятный отказ эффекта, не догадка.
MODEL_PARAMS = ("model",)


def register(registry):
    """Подключить операции к реестру адаптера (#461). Одна точка: воркер и
    реестр HTTP расходиться не могут -- добавил глагол сюда, кнопка в GUI
    жива. Add без цели (#470): имя присвоит сервис, target клика -- origin.
    Вызывает поднимающий веб service; проверки зовут явно."""
    # restart: рестарт не имеет своих params (force у веба нет, #460),
    # но глагол обязан быть зарегистрирован -- иначе POST из GUI отвечает
    # unsupported (#489: пропуск #464, эффект был, регистрации не было).
    registry.register("restart", ())
    registry.register("add", ADD_PARAMS, target=False)
    registry.register("update", UPDATE_PARAMS)
    registry.register("recycle", RUIN_PARAMS)
    registry.register("delete", RUIN_PARAMS)
    registry.register("wipe", RUIN_PARAMS)
    registry.register("model", MODEL_PARAMS)


def remote_workspace(origin, fetch=None):
    """workspace веб-операции из ЯВНОГО удалённого источника (#470).

    У веба нет рабочей копии: текст .mop/bootstrap.yaml берётся из origin
    (fetch -- manifest.fetch-подобный читатель), никаких cwd-чтений и
    локальных файлов сервера. Происхождение -- тем же видом, что собирал
    командлет (#334): source всегда "origin". Пусто (файла нет) -- честная
    половина: сервер снимет копию bootstrap'а, и удаление доедет."""
    from ..common import manifest
    if fetch is None:
        fetch = manifest.fetch
    got = fetch(origin) or {}
    text = got.get("bootstrap_text") or ""
    present = bool(text.strip())
    tasks = 0
    if present:
        try:
            tasks = len(manifest.play(text)[1])
        except ValueError:
            tasks = None
    return text, {"source": "origin", "commit": got.get("commit"),
                  "dirty": False, "tasks": tasks, "present": present}


def add_request(params):
    """params add -> (target, поля глагола). Target add -- сам origin:
    имени папета ещё нет, его присвоит сервис (#470)."""
    origin = params.get("origin")
    if not isinstance(origin, str) or not origin.strip():
        raise ValueError("add needs the origin parameter")
    return origin, {"origin": origin,
                    **({"branch": params["branch"]}
                       if params.get("branch") else {})}


def update_request(params):
    """params update -> (target, поля глагола). fresh -- ЯВНЫЙ bool
    (#470): молчаливого режима нет, отсутствующий False. Происхождение
    workspace решает операция: fresh без origin -- прежний, с origin --
    новый."""
    fresh = params.get("fresh")
    if not isinstance(fresh, bool):
        raise ValueError("update needs an explicit fresh (bool)")
    out = {"fresh": fresh}
    if params.get("origin") is not None:
        if not isinstance(params["origin"], str) or not params["origin"].strip():
            raise ValueError("update origin must be a non-empty origin")
        out["origin"] = params["origin"]


def model_request(params):
    """params model -> id модели (#468). Форма id -- ops.valid_model_id:
    отказ приёма до intent, кривой запрос не оставляет записи «что-то
    случилось». Само существование модели у прокси проверяет эффект."""
    model = params.get("model")
    if not ops.valid_model_id(model):
        raise ValueError(f"model needs a valid model id, got {model!r}")
    return model
    return out


def send_request(params):
    """Текст сообщения без расширения полномочий полями force/owner."""
    message = params.get("message")
    if set(params) != {"message"} or not isinstance(message, str) or not message.strip():
        raise ValueError("send needs only a non-empty message")
    return message


def normalized(request):
    """Нормализованный запрос -> хэш: сортированные ключи, без пробелов.

    Один и тот же смысл -- один хэш: порядок ключей JSON не должен делать
    из повторного клика «другой payload»."""
    return hashlib.sha256(json.dumps(request or {}, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def _quiet_transport(transport):
    """Транспорт восстановления: сбой read-запроса -- None, а не исключение.

    Донаблюдение при подъёме службы не имеет права уронить её старт; None
    (-- транспорт не дан) -- наблюдение пропускается, запись остаётся
    unknown."""

    class Quiet:
        def __call__(self, verb, **fields):
            try:
                return transport(verb, **fields)
            except Exception:
                return None
    return Quiet() if transport else None


# ── жизненный цикл (#471): обёртки общего слоя ───────────────────────────
# puppets.recycle/delete/wipe -- УЖЕ общие реализации (#458; CLI и gc зовут
# их же), здесь только структурный ответ: воркер читает ключи outcome(),
# вторая реализация алгоритма не пишется. Отказ владельца от ворот сервиса
# и отказ агента (живая tmux-сессия у wipe) едут bus.Refused/RuntimeError --
# их ловит _run и пишет failed; снос по живой сессии -- работа safety и
# агента, не своя матрица. force веб не берёт: ни одна кнопка его не шлёт.
# Логин -- принципал HTTP-сессии: операции #458 зовут им login=, ворота
# владения сервиса (#40) видят человека, а не машину.


def _outcome_of(got, name):
    """Ответ общей функции -> вердикт outcome(). replaced=False (спека не
    изменилась, нового прогона не будет) -- accepted: эффект был,
    наблюдения у него нет. Ключ name из ответов узла не переопределяет
    вердиктный -- цель операции уже названа."""
    if not isinstance(got, dict):
        return ops.outcome(ops.ACCEPTED, name)
    facts = {k: v for k, v in got.items() if k not in ("name", "replaced")}
    if got.get("replaced") is False:
        return ops.outcome(ops.ACCEPTED, name, **facts)
    return ops.outcome(ops.REGISTERED, name, **facts)


# Отказы, которые суть вердикт, а не сбой: ворота сервиса (#40) и живая
# tmux-сессия у wipe (#40 агента) отвечают внятным «нет», и CLI показывал
# бы их как отказ операции. Неизвестность эффекта здесь ложной быть не
# может -- обёртки ловят только эти два текста, остальное летит выше и
# остаётся failed.
_LIVE_SESSION = "tmux session is alive"


def _guarded(target, fn):
    """Общий вызов с переводом внятных отказов в refused-вердикт."""
    try:
        return fn()
    except (bus.Refused, RuntimeError) as e:
        text = str(e)
        kind = (ops.REFUSED if _LIVE_SESSION in text
                or "led by" in text or "held by" in text
                else None)
        if kind is None:
            raise
        return ops.outcome(kind, target, error=text)


def _wrap_recycle(target, login):
    return _outcome_of(_guarded(target, lambda: puppets.recycle(
        target, force=False)), target)


def _wrap_delete(target, login):
    return _outcome_of(_guarded(target, lambda: puppets.delete(
        target, force=False)), target)


def _wrap_wipe(target, login):
    # Голый wipe -- остановленному (#377: узел любой последней аллокации);
    # живая сессия -- отказ агента, для неё recycle.
    return _outcome_of(_guarded(target, lambda: puppets.wipe(
        puppets.node_of(target, running=False), target, force=False)), target)


def _wrap_model(target, login, model, now=None, sleep=None, observe_wait=30):
    # Смена модели (#468) -- общий ops.switch_model (#468), не своя копия:
    # валидация формы, no-op по свежей телеметрии и подтверждение сессией
    # живут там. Часы и сон -- воркера (застрявший такт теста не заставил
    # бы наблюдение жить по-настоящему); отказ эффекта (диалог не
    # закрылся) ловит _guarded.
    def node_call(node, verb, **fields):
        # node-образный транспорт поверх шины: read-глагол узла своим
        # проектом (оператор -- admin-проект, права как у mop admin).
        return bus.request(node, verb, **fields)
    return _guarded(target, lambda: ops.switch_model(
        node_call, puppets.node_of(target), target, model,
        now=now or time.time, sleep=sleep or time.sleep,
        observe_wait=observe_wait))


def _wrap_send(target, login, message):
    """Доставка от HTTP-принципала: факты и проект не берутся из запроса."""
    if not login or not isinstance(target, str) or not target.startswith(puppets.JOB_PREFIX):
        return ops.outcome(ops.REFUSED, target, error="send needs an authenticated actor and a puppet target")
    try:
        spec = puppets._cluster("spec", name=target) or {}
        origin = (spec.get("meta") or {}).get("origin")
        if not origin:
            return ops.outcome(ops.REFUSED, target, error="cannot determine the target project")
        project = ops.target_project(origin)
        if not project or project == bus.ADMIN:
            return ops.outcome(ops.REFUSED, target, error="cannot determine the target project")
        node = puppets.node_of(target)

        def node_call(node, verb, **fields):
            return bus.request(node, verb, project=project, **fields)

        facts = node_call(node, "state", name=target)
        safe, why = ops.safety(target, project, owner=login, facts=facts)
        if not safe:
            return ops.outcome(ops.REFUSED, target, error=why)
    except (bus.BusError, bus.Refused, LookupError):
        return ops.outcome(ops.REFUSED, target, error="cannot inspect the target before sending")
    try:
        # У веб-чата нет инбокса на шине: from_name не выдаёт выдуманный
        # адрес за ответный. HTTP-принципал едет владельцем, force запрещён.
        got = ops.send_to_puppet(node_call, node, target, message, owner=login, force=False)
    except bus.Refused as e:
        return ops.outcome(ops.REFUSED, target, error=str(e))
    except Exception:
        # Запрос мог дойти до сокета до потери ответа: повторять нельзя.
        return ops.outcome(ops.UNKNOWN, target, error="message delivery could not be confirmed")
    if got.get("error"):
        return ops.outcome(ops.REFUSED, target, error=got["error"])
    if not isinstance(got.get("msg_id"), str) or not got["msg_id"].strip():
        return ops.outcome(ops.UNKNOWN, target, error="message delivery could not be confirmed")
    return ops.outcome(ops.REGISTERED, target)


# Регистрация disruptive (#460): снос клона, джоба или тела под занятой
# сессией -- отказ даже со своей арендой; _run зовёт safety им.
DISRUPTIVE = frozenset({"restart", "recycle", "delete", "wipe"})


class Worker:
    """Приём и исполнение веб-операций над устойчивым журналом.

    Один замок на каталог: приём -- редкое событие, а спорить о файле
    дороже, чем подождать. Все записи идут через _append (fsync), чтение --
    снапшотом из памяти, который собирается при старте и держится тем же
    замком."""

    LIMIT = QUEUE_LIMIT

    def __init__(self, root=None, now=None, retention_days=RETENTION_DAYS,
                 payload_days=PAYLOAD_DAYS, transport=None,
                 observe_wait=OBSERVE_WAIT, observe_poll=OBSERVE_POLL,
                 sleep=time.sleep, registry=None, workspace=None):
        import tempfile
        from ..common import paths
        self._root = root or paths.local("operations")
        self._now = now or time.time
        self.retention_days, self.payload_days = retention_days, payload_days
        self.observe_wait, self.observe_poll = observe_wait, observe_poll
        self._sleep = sleep              # наблюдение опрашивает им же
        self._transport = transport      # read-only: донаблюдение (#464)
        # Источник workspace веб-операций (#470): origin -> (текст,
        # происхождение), явный удалённый источник; None -- дефолт
        # remote_workspace (manifest.fetch).
        self._workspace = workspace
        # Реестр заведённых проектов (callable -> set origin'ов): gate add
        # (#470). None -- не подключён, add отказывает (fail closed).
        self._registry = registry
        self._lock = threading.Lock()
        self._log = os.path.join(self._root, "ops.jsonl")
        self._ops = {}          # id -> запись
        self._by_key = {}       # (actor, ключ) -> id
        self._open = []         # незавершённые id, порядок приёма
        os.makedirs(os.path.join(self._root, "payload"), exist_ok=True)
        self._load()
        self._recover()

    # ── приём ───────────────────────────────────────────────────────────
    def submit(self, actor, verb, target, request, transport=None,
               safety=None, role="admin", context=None, effect=None,
               key=None):
        """Принять операцию. -> запись-ответ.

        Роль -- ДО первого эффекта: не admin -- отказ приёма, эффектов нет.
        key -- клиентский ключ идемпотентности (actor+key -- дедуп); без
        ключа каждая подача -- новая операция, дубли ловит только тот же
        ключ. effect -- вызов исполнителя (по умолчанию -- библиотечный
        handler #458 через transport); safety -- ops.safety-подобный
        предикат. Хранилище недоступно -- операция не запускается."""
        if role != "admin":
            return {"status": "refused", "error": "admin only"}
        digest = normalized(request)
        try:
            return self._accept(actor, verb, target, request, digest,
                                transport, safety, effect, context, key)
        except OSError as e:
            # Хранилище недоступно -- операция НЕ запускается: тихо
            # продолжить без записи -- потерять результат (#476).
            return {"status": "failed", "error": f"operations storage: {e}"}

    def _accept(self, actor, verb, target, request, digest,
                transport, safety, effect, context, key):
        request = request or {}
        # add (#470): цели нет -- ею становится сам origin; заводить можно
        # только заведённый проект. update: fresh -- явный bool. Всё до
        # intent: кривой запрос не оставляет «что-то случилось».
        try:
            if verb == "add":
                target, _ = add_request(request)
                why = self._registry_refusal(target)
                if why:
                    return {"status": "refused", "error": why}
            elif verb == "update":
                update_request(request)
            elif verb == "model":
                model_request(request)
            elif verb == "send":
                send_request(request)
        except ValueError as e:
            return {"status": "refused", "error": str(e)}
        idem = key or f"{verb}:{target}:{digest[:8]}"
        with self._lock:
            known = self._by_key.get((actor, idem))
            if known is None and any(t["actor"] == actor and t["key"] == idem
                                     for t in self._tombstones()):
                # Ключ уже жил и был очищен: молча выдать его за новый --
                # потерять границу повтора; отказ честнее (#476).
                return {"status": "refused",
                        "error": "this key was purged; take a new one"}
            if known is not None:
                rec = self._ops[known]
                if rec["hash"] == digest:
                    # Тот же запрос под тем же ключом: не второй запуск,
                    # а тот же результат.
                    return {k: rec[k] for k in ("op", "status")}
                return {"status": "refused",
                        "error": "the same key carries another payload"}
            if len(self._open) >= self.LIMIT:
                return {"status": "queue-full",
                        "error": "the operations queue is full"}
            op = f"op-{int(self._now() * 1000)}-{len(self._ops):04d}"
            rec = {"op": op, "actor": actor, "verb": verb, "target": target,
                   "status": "queued", "at": self._now(),
                   "key": idem, "hash": digest}
            # Intent ДО эффекта: крах ниже оставит честную запись, а не
            # «что-то случилось».
            self._append(rec)
            self._ops[op] = rec
            self._by_key[(actor, idem)] = op
            self._open.append(op)
            self._payload(op, {"operation": verb, "target": target,
                               "request": request})
        return self._run(rec, transport, safety, effect, context)

    # ── исполнение ──────────────────────────────────────────────────────
    def _run(self, rec, transport, safety, effect, context):
        """Довести операцию до исхода. Начатое -- до этапа и аудит.

        Роль проверена приёмом, safety -- до первого эффекта: отказ
        помечает запись и не зовёт транспорт вовсе. По цели уже идёт
        операция -- эта остаётся queued (сериализация по устойчивой
        идентичности цели #476), её доведёт следующий вызов/drive."""
        audit = ops.record(rec["verb"], rec["target"], None, rec["actor"])
        with self._lock:
            ahead = [o for o in self._open
                     if self._ops[o]["target"] == rec["target"] and o != rec["op"]]
        if ahead:
            return {**rec, "status": "queued", "audit": audit}
        self._mark(rec, "running")
        if safety is not None:
            # Сигнатура -- ops.safety (#460): имя, проект цели, принципал.
            # Разрушительность -- словарь регистрации (#471), не решение
            # вызывающего.
            ok, why = safety(rec["target"], self._project_of(rec),
                             owner=rec["actor"],
                             disruptive=rec["verb"] in DISRUPTIVE)
            if not ok:
                self._mark(rec, "refused", error=why)
                return {**rec, "status": "refused", "error": why, "audit": audit}
        try:
            if effect is None:
                effect = self._library_effect(rec, transport, context)
            got = effect()
        except Exception as e:
            self._mark(rec, "failed", error=str(e))
            return {**rec, "status": "failed", "error": str(e), "audit": audit}
        kind = (got or {}).get("outcome") if isinstance(got, dict) else None
        status = _FROM_OUTCOME.get(kind, "partial")
        # Факты наблюдения (#464) и присвоенное имя add (#470) -- в журнал:
        # по ним карточка и recovery отвечают, что именно увидено.
        facts = {k: (got or {}).get(k) for k in ("alloc", "restarts", "name")
                 if (got or {}).get(k) is not None}
        self._mark(rec, status, error=(got or {}).get("error"), **facts)
        # Освободив цель, двинуть следующую по ней (queued -> исход).
        self._drive(rec["target"], transport, safety, effect, context)
        return {**rec, "status": status,
                "error": (got or {}).get("error"),
                "audit": audit}

    def _drive(self, target, transport, safety, effect, context):
        """Исполнить следующую queued-операцию по цели после завершения."""
        with self._lock:
            waiting = [o for o in self._open
                       if self._ops[o]["target"] == target
                       and self._ops[o]["status"] == "queued"]
        for o in waiting:
            self._run(self._ops[o], transport, safety, effect, context)

    def _library_effect(self, rec, transport, context):
        """Эффект библиотечным workflow #458: воркер не держит копий.

        Рестарт -- наблюдаемым режимом (#464): база снимка пишется в
        журнал ДО эффекта, наблюдение -- часть операции; алгоритм --
        в mop/common/ops.py, здесь вызов.

        add/update (#470): workspace -- из ЯВНОГО удалённого источника
        (self._workspace, по умолчанию remote_workspace), никогда не cwd и
        не локальный файл сервера. Update читает спеку здесь, до эффекта:
        origin прежнего репозитория решает, чей workspace качать, и в
        операцию едет один раз (не спрашивать вторично).

        Жизненный цикл (#471) -- те же общие функции puppets.recycle/
        delete/wipe, что и у CLI: обёртка кладёт их ответ в структурный
        вердикт outcome(), эффектов-копий здесь нет.
        """
        fn = {"restart": ops.restart, "add": ops.add, "update": ops.update,
              "recycle": _wrap_recycle, "delete": _wrap_delete,
              "wipe": _wrap_wipe, "model": _wrap_model,
              "send": _wrap_send}.get(rec["verb"])
        if fn is None:
            raise RuntimeError(f"no library operation for {rec['verb']}")
        base = rec.get("baseline")
        if rec["verb"] == "restart" and base is None:
            try:
                base = ops.task_snapshot(transport, rec["target"])
            except Exception:
                base = None
            if base is not None:
                # База наблюдения -- ДО эффекта: переживает крах службы.
                self._mark(rec, rec["status"], baseline=base)
        # transport -- глаголы сервиса кластера (transport(verb, **fields)),
        # как у #458; контекст #457 передаётся явно, без env потока. Две
        # ветки -- двумя именами: лямбда с тернарником внутри ловила бы
        # else-ветку лениво (тело `lambda: A if ... else B` -- само B),
        # и #476 это не прощупал -- ветка была мертва (fn None).
        if rec["verb"] == "restart":
            call = lambda: fn(transport, rec["target"], login=rec["actor"],
                              force=False, observe=True, before=base,
                              observe_wait=self.observe_wait,
                              sleep=lambda s: self._sleep(self.observe_poll))
        else:
            # Ветви по семействам: add/update (#470) строят вход в _lifecycle,
            # жизненный цикл (#471) зовёт общие функции напрямую. Две ветки --
            # двумя именами: лямбда с тернарником внутри ловила бы else-ветку
            # лениво (тело `lambda: A if ... else B` -- само B), и #476 это
            # не прощупал -- ветка была мертва (fn None).
            if rec["verb"] in ("add", "update"):
                call = lambda: self._lifecycle(fn, rec, transport)
            elif rec["verb"] == "send":
                message = send_request(self._payload_read(rec["op"]).get("request", {}))
                call = lambda: fn(rec["target"], rec["actor"], message)
            elif rec["verb"] == "model":
                # Смена модели (#468): id заказа читается из payload
                # запроса один раз, здесь; эффект -- общий слой с часами
                # и сном воркера, как у наблюдаемого рестарта (#464).
                model = self._payload_read(rec["op"]).get("request", {}) \
                    .get("model")
                call = lambda: fn(rec["target"], rec["actor"], model,
                                  now=self._now,
                                  sleep=lambda s: self._sleep(self.observe_poll),
                                  observe_wait=self.observe_wait)
            else:
                call = lambda: fn(rec["target"], login=rec["actor"])
        if context is not None:
            from ..common import context as _c
            return lambda: _c.call_in_context(context, call)
        return call

    def _source(self, origin):
        """Выкачка workspace (#470): подмена проверками, по умолчанию --
        manifest.fetch через remote_workspace."""
        if self._workspace is not None:
            return self._workspace(origin)
        return remote_workspace(origin)

    def _lifecycle(self, fn, rec, transport):
        """add/update (#470): построить вход, позвать общую операцию.

        add: workspace из origin запроса, branch -- из него же; присвоенное
        имя -- факт записи. update: спека читается ДО эффекта (чей origin
        -- решает) и едет в операцию один раз; fresh -- явный bool запроса;
        workspace -- из нового или прежнего origin."""
        request = self._payload_read(rec["op"]).get("request") or {}
        if rec["verb"] == "add":
            text, sent = self._source(rec["target"])
            return fn(transport, rec["target"], (text, sent),
                      branch=request.get("branch"))
        spec = transport("spec", name=rec["target"]) or {}
        old_origin = (spec.get("meta") or {}).get("origin")
        new_origin = (request.get("origin")
                      if request.get("origin") is not None else old_origin)
        text, sent = self._source(new_origin)
        return fn(transport, rec["target"], origin=request.get("origin"),
                  fresh=bool(request.get("fresh")), workspace=(text, sent),
                  force=False, login=rec["actor"], spec=spec)

    def _payload_read(self, op):
        """Payload операции: запрос без ответов транспорта (#470)."""
        try:
            with open(os.path.join(self._root, "payload", f"{op}.json"),
                      encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _registry_refusal(self, origin):
        """Gate add (#470): origin обязан быть в реестре сервера. None --
        не подключён -- отказ тоже (fail closed): тихо пропустить значит
        завести проект, которого нет ни у шины, ни у плейбука."""
        if self._registry is None:
            return f"no project {puppets.project_of(origin)} in the registry"
        known = self._registry() or set()
        if origin not in known:
            return (f"no project {puppets.project_of(origin)} in the registry; "
                    f"what the pool serves: mop project list")
        return None

    def _project_of(self, rec):
        """Проект цели операции: из payload запроса его нет, пока воркер
        наверху не передал -- None (safety его не читает для решения)."""
        return None

    # ── чтение ──────────────────────────────────────────────────────────
    def read(self, actor, op=None, role=None):
        """GET результатов: актуальная admin-роль; свой actor.

        actor-supplied ID чужого -- нет записи: ID не право. role != admin
        -- None (пониженный не читает). Без op -- список своих операций,
        без payload исполнителя."""
        if role is not None and role != "admin":
            return None
        if op is None:
            with self._lock:
                mine = [dict(r) for r in self._ops.values() if r["actor"] == actor]
            return [{k: v for k, v in r.items()
                     if k not in ("hash", "key")} for r in mine]
        with self._lock:
            rec = self._ops.get(op)
        if not rec or rec["actor"] != actor:
            return None
        return {k: v for k, v in rec.items() if k not in ("hash", "key")}

    # ── устойчивость ────────────────────────────────────────────────────
    def _append(self, rec):
        """Запись в журнал с fsync: тихо продолжать без неё нельзя."""
        with open(self._log, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _mark(self, rec, status, error=None, **facts):
        """Переход с записью в журнал; завершённая уходит из очереди.

        facts (baseline/alloc/restarts) -- факты операции, в журнал: без
        них донаблюдению восстановления не от чего отсчитывать сигнал."""
        rec = self._ops[rec["op"]]
        rec["status"] = status
        if error is not None:
            rec["error"] = error
        rec.update({k: v for k, v in facts.items() if v is not None})
        if status in TERMINAL and rec["op"] in self._open:
            self._open.remove(rec["op"])
        self._append({**rec, "transition": status})

    def _payload(self, op, data):
        """Payload исполнителя: отдельный файл, свой срок, приватно."""
        path = os.path.join(self._root, "payload", f"{op}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def _load(self):
        """Журнал -> память: последняя запись о каждой операции."""
        if not os.path.exists(self._log):
            return
        with open(self._log, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                op = rec.get("op")
                if not op:
                    continue
                prev = self._ops.get(op, {})
                prev.update(rec)
                self._ops[op] = prev
                self._by_key.setdefault((rec.get("actor"), rec.get("key")), op)
                if prev["status"] not in TERMINAL:
                    if op not in self._open:
                        self._open.append(op)

    def _recover(self):
        """Подъём службы: незавершённое -- unknown, без переигрывания.

        Исключение -- наблюдаемый рестарт (#464) с журнальной базой: до
        краша он мог и сойтись. Донаблюдение -- read-only тем же сигналом
        (ops.observe_restart), эффект не переигрывается никогда: unknown
        запрещает автоматический повтор (#476). Сошлось -- честный исход,
        не сошлось -- остаётся unknown с причиной."""
        for op in list(self._open):
            rec = self._ops[op]
            if rec["status"] not in ("queued", "running"):
                continue
            if rec["verb"] == "restart" and rec.get("baseline") \
                    and self._transport is not None:
                got = ops.observe_restart(_quiet_transport(self._transport),
                                          rec["target"], rec["baseline"],
                                          observe_wait=self.observe_wait,
                                          sleep=lambda s: self._sleep(self.observe_poll),
                                          clock=self._now)
                if got.get("outcome") == "registered":
                    self._mark(rec, "succeeded",
                               alloc=got.get("alloc"),
                               restarts=got.get("restarts"))
                    continue
                self._mark(rec, "unknown",
                           error=got.get("error")
                           or "the web service restarted mid-operation")
                continue
            self._mark(rec, "unknown",
                       error="the web service restarted mid-operation")

    # ── служебное для проверок (#476: чистые механизмы) ────────────────
    def _begin_for_test(self, actor, verb, target, request):
        """Запись, остановленная на running: крах посреди операции."""
        with self._lock:
            op = f"op-{int(self._now() * 1000)}-{len(self._ops):04d}"
            rec = {"op": op, "actor": actor, "verb": verb, "target": target,
                   "status": "running", "at": self._now(),
                   "key": f"{verb}:{target}", "hash": normalized(request)}
            self._append(rec)
            self._ops[op] = rec
            self._by_key[(actor, rec["key"])] = op
            self._open.append(op)
        return op

    def _retryable(self, rec):
        """Разрешён ли автоматический повтор: unknown -- никогда."""
        return bool(rec) and rec.get("status") == "failed"

    # ── retention и tombstone (#476) ────────────────────────────────────
    def _purge(self, before=None):
        """Очистка завершённых записей старше срока.

        Дедупликация не обещана навсегда: срок вышел -- записи нет. Но и
        молча считать старый ключ новым безопасным запросом нельзя: ключ
        уходит в tombstone (отдельная строка журнала, без payload), и тот
        же ключ после очистки -- отказ, а не «новая операция»."""
        cut = self._now() - self.retention_days * 86400 if before is None else before
        with self._lock:
            gone = [op for op, r in self._ops.items()
                    if r["status"] in TERMINAL and r["at"] < cut]
            for op in gone:
                rec = self._ops.pop(op)
                self._by_key.pop((rec["actor"], rec["key"]), None)
                self._append({"op": op, "key": rec["key"], "actor": rec["actor"],
                              "tombstone": True, "at": self._now()})
                self._drop_payload(op)

    def _drop_payload(self, op):
        """Payload исполнителя живёт короче записей: срок вышел -- файла нет."""
        try:
            os.remove(os.path.join(self._root, "payload", f"{op}.json"))
        except OSError:
            pass

    def _tombstones(self):
        """Ключи, уже жившие в этом журнале: после очистки -- только факт,
        что ключ был, без содержания."""
        out = []
        if not os.path.exists(self._log):
            return out
        with open(self._log, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("tombstone"):
                    out.append({"key": rec.get("key"), "actor": rec.get("actor")})
        return out

    def _stages(self, op):
        """Структурные этапы операции из журнала: [queued, running, исход].

        Читается проверками и будущим UI: переходы -- часть модели #476,
        а не внутренняя деталь."""
        out = []
        if not os.path.exists(self._log):
            return out
        with open(self._log, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("op") != op:
                    continue
                if "transition" in rec:
                    if not out or out[-1] != rec["transition"]:
                        out.append(rec["transition"])
                elif not out:
                    out.append(rec.get("status", "queued"))
        return out or ([self._ops[op]["status"]] if op in self._ops else [])

    def _audit(self):
        """Аудит-записи операций: ops.record на каждую."""
        return [ops.record(r["verb"], r["target"], None, r["actor"])
                for r in self._ops.values()]

    def _serialized(self, target, op):
        """Идёт ли операция по цели после данной: очередь по цели."""
        with self._lock:
            ids = [o for o in self._open if self._ops[o]["target"] == target]
        return op in ids

    def _blocked(self, target, op):
        with self._lock:
            return op not in self._open
