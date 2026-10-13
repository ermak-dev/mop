"""Исполнитель веб-операций: тонкий слой над общим workflow (#499).

Дашборд действует (карточка папета #462, кнопки #461): клик человека
должен выполняться библиотечным workflow #458 под инвариантом #460.
Воркер #476 держал собственную копию «жизни операции» -- журнал
ops.jsonl, payload-файлы, восстановление, очередь по цели -- и копия
разошлась с реальностью (#497: вечный accepted, 409 часами). Правды
две и обе уже есть: workflow #458 -- один на CLI и веб, аудит пула --
ops.record и /api/journal. Воркер #499 -- только исполнение: run ->
вердикт ops.*, тем же транспортом, что у CLI. Ни очереди, ни замка
цели, ни журнала: приём и статусы -- слой адаптера (#499 adminapi),
дюрабельности «клик -> вердикт» через рестарт службы нет -- честный
unknown со ссылкой на журнал (#499 adminapi.result).

Без unbounded потоков: run синхронен, поток операции даёт адаптер.
"""
import threading
import time

from ..common import bus, ops, puppets

# Наблюдение рестарта (#464): срок и шаг опроса настраиваются атрибутами
# воркера, значения -- заведённые константы установки.
OBSERVE_WAIT = 60            # сколько наблюдать рестарт
OBSERVE_POLL = 2             # шаг опроса наблюдения

# Срок эффекта (#497): зависший эффект (bus-запрос без ответа -- мёртвый
# узел-получатель) иначе держит цель вечно. Значение -- настройка
# MOP_WEB_EFFECT_TIMEOUT (дефолт 900 с, запас над долгим add ~540 с).
# С #499 дедлайн живёт в run: эффект -- под сроком, вышедший без ответа --
# неизвестность, не сбой: он мог дойти до цели.
EFFECT_TIMEOUT = 900

# Исключение срока (#497): не RuntimeError -- _guarded не переводит его в
# refused; run ловит только его -- в честный unknown «effect timed out».
class _TimedOut(Exception):
    pass

# Модель статусов -- прежняя (#476): точные переходы, набор терминалов
# и карта исхода workflow #458 в статус операции принадлежат воркеру,
# адаптер #499 их переиспользует. unknown -- не failed: эффекта могли и
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


def effective_target(action, target, params):
    """Устойчивая идентичность операции и её полосы (#569): target, а
    для операции без цели -- origin из params. Одна точка правила:
    дедуп-ключ адаптера и полоса воркера считаются одной функцией, и
    два add одного origin -- одна полоса (одна очередь к цели), разных
    origin'ов -- независимые. Адаптер делегирует сюда."""
    if target is None:
        return (params or {}).get("origin")
    return target


def status_of(verdict):
    """Вердикт операции -> терминальный статус #476. Одна точка карты для
    обоих видов: вердикт воркера (status) и вердикт workflow #458
    (outcome). Чужая форма -- failed: тишина не бывает успехом."""
    if isinstance(verdict, dict):
        if verdict.get("status") in TERMINAL:
            return verdict["status"]
        got = _FROM_OUTCOME.get(verdict.get("outcome"))
        if got is not None:
            return got
    return "failed"

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


def default_transport(resolver=None):
    """Глаголы сервиса кластера для библиотечных операций воркера (#496).

    Драйвер веб-службы передаёт его в run: без транспорта эффект падал
    «'NoneType' object is not callable» -- воркер транспорта не строит,
    его передают. Субъект -- ПРОЕКТА ЦЕЛИ (#498): resolver(name) -> проект
    из фактов джоба; у add проект решает регистрируемый origin (#470).
    Без резолвера -- субъект процесса: чужой проект получит belongs-отказ
    словами сервиса, но правильный вход -- резолвер воркера (#460)."""

    def call(verb, name=None, **fields):
        project = None
        if resolver is not None:
            project = (resolver(fields.get("origin"))
                       if verb == "add" and fields.get("origin")
                       else resolver(name) if name else None)
        # name едет в глагол, а не остаётся параметром резолвера:
        # библиотека зовёт transport("restart", name=...), и проглотить
        # его здесь -- сервис получит глагол без поля.
        if name is not None:
            fields["name"] = name
        return bus.call_cluster(verb, project=project, **fields)
    return call


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
    """params update -> поля глагола. fresh -- ЯВНЫЙ bool (#470):
    молчаливого режима нет, отсутствующий False. Происхождение workspace
    решает операция: fresh без origin -- прежний, с origin -- новый."""
    fresh = params.get("fresh")
    if not isinstance(fresh, bool):
        raise ValueError("update needs an explicit fresh (bool)")
    out = {"fresh": fresh}
    if params.get("origin") is not None:
        if not isinstance(params["origin"], str) or not params["origin"].strip():
            raise ValueError("update origin must be a non-empty origin")
        out["origin"] = params["origin"]
    return out


def model_request(params):
    """params model -> id модели (#468). Форма id -- ops.valid_model_id:
    отказ приёма до intent, кривой запрос не оставляет записи «что-то
    случилось». Само существование модели у прокси проверяет эффект."""
    model = params.get("model")
    if not ops.valid_model_id(model):
        raise ValueError(f"model needs a valid model id, got {model!r}")
    return model


def send_request(params):
    """Текст сообщения без расширения полномочий полями force/owner."""
    message = params.get("message")
    if set(params) != {"message"} or not isinstance(message, str) or not message.strip():
        raise ValueError("send needs only a non-empty message")
    return message


# ── жизненный цикл (#471): обёртки общего слоя ───────────────────────────
# puppets.recycle/delete/wipe -- УЖЕ общие реализации (#458; CLI и gc зовут
# их же), здесь только структурный ответ: воркер читает ключи outcome(),
# вторая реализация алгоритма не пишется. Отказ владельца от ворот сервиса
# и отказ агента (живая tmux-сессия у wipe) едут bus.Refused/RuntimeError --
# их ловит run и пишет failed; снос по живой сессии -- работа safety и
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
# может -- обёртки ловят только эти тексты, остальное летит выше и
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


def _wrap_send(target, login, message, context=None):
    """Доставка от HTTP-принципала: факты и проект не берутся из запроса."""
    if not login or not isinstance(target, str) or not target.startswith(puppets.JOB_PREFIX):
        return ops.outcome(ops.REFUSED, target, error="send needs an authenticated actor and a puppet target")
    project = None   # подтверждается спекой ниже; отказ до -- честный None
    try:
        spec = puppets._cluster("spec", name=target) or {}
        origin = (spec.get("meta") or {}).get("origin")
        if not origin:
            return ops.outcome(ops.REFUSED, target, error="cannot determine the target project")
        project = ops.target_project(origin)
        if context is not None:
            # Аудит-факт (#575): спека подтвердила проект ДО доставки --
            # зависшая доставка его больше не отнимает.
            context["project"] = project
        if not project or project == bus.ADMIN:
            return ops.outcome(ops.REFUSED, target, error="cannot determine the target project")
        node = puppets.node_of(target)

        def node_call(node, verb, **fields):
            return bus.request(node, verb, project=project, **fields)

        facts = node_call(node, "state", name=target)
        safe, why = ops.safety(target, project, owner=login, facts=facts)
        if not safe:
            return ops.outcome(ops.REFUSED, target, error=why,
                               project=project)
    except (bus.BusError, bus.Refused, LookupError):
        # Проект уже подтверждён спекой -- везём его аудиту (#575)
        return ops.outcome(ops.REFUSED, target,
                           error="cannot inspect the target before sending",
                           project=project)
    try:
        # У веб-чата нет инбокса на шине: from_name не выдаёт выдуманный
        # адрес за ответный. HTTP-принципал едет владельцем, force запрещён.
        got = ops.send_to_puppet(node_call, node, target, message, owner=login, force=False)
    except bus.Refused as e:
        return ops.outcome(ops.REFUSED, target, error=str(e),
                           project=project)
    except Exception:
        # Запрос мог дойти до сокета до потери ответа: повторять нельзя.
        return ops.outcome(ops.UNKNOWN, target,
                           error="message delivery could not be confirmed",
                           project=project)
    if got.get("error"):
        if got.get("timeout"):
            # Неопределённая доставка (#574, review-3 п.5): тело молчит,
            # сообщение могло доехать -- это НЕ подтверждённый отказ, и
            # повтор автоматически не делается: он мог бы продублировать
            # уже выполненное. Проект аудита остаётся при вердикте (#575).
            return ops.outcome(ops.UNKNOWN, target, error=got["error"],
                               project=project)
        return ops.outcome(ops.REFUSED, target, error=got["error"],
                           project=project)
    if not isinstance(got.get("msg_id"), str) or not got["msg_id"].strip():
        return ops.outcome(ops.UNKNOWN, target,
                           error="message delivery could not be confirmed",
                           project=project)
    return ops.outcome(ops.REGISTERED, target, project=project)


# Регистрация disruptive (#460): снос клона, джоба или тела под занятой
# сессией -- отказ даже со своей арендой; run зовёт safety им.
DISRUPTIVE = frozenset({"restart", "recycle", "delete", "wipe"})


class _LaneToken:
    """Токен-держатель полосы цели (#569). Заводится _reserve ДО старта
    нитки эффекта; release идемпотентен и снимает токен из слота цели.
    held -- флаг жизни: пока True, полоса занята этим токеном (нитка
    эффекта ещё не кончилась), drain другой операции его ждёт. Пустой
    токен (target None, lanes None) -- операция без полосы (add без
    origin): release -- no-op."""

    def __init__(self, target, lock=None, lanes=None):
        self.target = target
        self.held = True
        self._lock, self._lanes = lock, lanes

    def release(self):
        """Снять себя из слота; повторный вызов -- no-op."""
        if not self.held:
            return
        self.held = False
        if self._lanes is not None:
            with self._lock:
                slot = self._lanes.get(self.target)
                if slot is not None:
                    slot.discard(self)
                    if not slot:
                        self._lanes.pop(self.target, None)


class Worker:
    """Исполнение веб-операции: run(verb, target, params, login) -> вердикт.

    Синхронный вызов в потоке адаптера (#499): ни очереди, ни журнала,
    ни своего статуса -- всё это держит адаптер. Воркер держит только
    настройки наблюдения (#464) и источник workspace (#470) и зовёт
    общие функции #458 тем же транспортом, что CLI."""

    def __init__(self, now=None, transport=None,
                 observe_wait=OBSERVE_WAIT, observe_poll=OBSERVE_POLL,
                 effect_timeout=None, sleep=time.sleep, registry=None,
                 workspace=None):
        self._now = now or time.time
        self.observe_wait, self.observe_poll = observe_wait, observe_poll
        if effect_timeout is None:
            from ..common import config
            effect_timeout = max(1, config.num("MOP_WEB_EFFECT_TIMEOUT"))
        self.effect_timeout = effect_timeout  # срок эффекта (#497)
        self._sleep = sleep              # наблюдение опрашивает им же
        self._transport = transport      # не используется run'ом: хвост
        # #476-эпохи для читателей; сам run транспорт получает параметром
        # (default_transport(resolver), #496/#498)
        # Источник workspace веб-операций (#470): origin -> (текст,
        # происхождение), явный удалённый источник; None -- дефолт
        # remote_workspace (manifest.fetch).
        self._workspace = workspace
        # Реестр заведённых проектов (callable -> set origin'ов): gate add
        # (#470). None -- не подключён, add отказывает (fail closed).
        self._registry = registry
        # Полосы целей (#569): target -> живые нитки-держатели эффектов.
        # Слот живёт до фактического конца нитки; см. _limited/_drain.
        self._lanes = {}
        self._lanes_lock = threading.Lock()

    # ── исполнение ──────────────────────────────────────────────────────
    def run(self, verb, target, params, login, transport=None,
            safety=None, role="admin", prepare=None, context=None):
        """Исполнить операцию -> вердикт-словарь #476 ({status, error, ...}).

        Роль -- ДО первого эффекта: не admin -- отказ, эффектов нет.
        safety -- ops.safety-подобный предикат ДО эффекта; None -- общий
        слой сам решает (send проверяет facts своей safety-функцией).
        prepare -- сборка предиката и транспорта ИЗ ЖИВОЙ ШИНЫ (#566):
        вызывается внутри срока эффекта (#569), до ворот и эффекта;
        (safety, transport, None) | (None, None, отказ) -- отказ до
        эффекта, с причиной, без мутаций. Что собирает prepare, то и
        исполняется: префетч -- часть локального workflow, а не фоновая
        подготовка, и виснуть он обязан под тем же дедлайном, что и
        эффект, -- иначе его bus-запрос держал бы полосу цели без срока.
        transport -- глаголы сервиса кластера (#496/#498); воркер своего
        не строит. Вердикт синхронен: поток операции даёт адаптер (#499).
        context (#575) -- словарь аудита операции: эффекты (send/add)
        кладут в него подтверждённые факты (проект спеки/реестра,
        доверенное имя регистрации) В МОМЕНТ факта, до зависания
        позднейших шагов; словарь принадлежит вызову, не воркеру, --
        параллельные операции его не смешивают.
        Полоса цели (#569): срок #497 ограничивает ОТВЕТ, не жизнь
        эффекта -- нитка эффекта переживает таймаут и остаётся держателем
        полосы (rider). run той же цели сперва ждёт живых хвостов в
        пределах того же срока; не дождался -- отказ: эффект начат не
        был, это не «цель свободна» и не автоповтор. Ответ ограничен
        терпением + сроком эффекта, вечного accepted нет."""
        if role != "admin":
            return {"status": "refused", "error": "admin only"}

        def whole():
            # Весь локальный workflow одной ниткой под одним сроком:
            # сбор фактов (#566), ворота, эффект. Что-то одно висит --
            # дедлайн терминирует ответ, нитка остаётся держателем полосы.
            gate_pred, gate_tr = safety, transport
            if prepare is not None:
                gate_pred, gate_tr, refusal = prepare()
                if refusal is not None:
                    return {"status": "refused", "error": refusal}
            if gate_pred is not None:
                ok, why = gate_pred(target, self._project_of(verb, target),
                                    owner=login,
                                    disruptive=verb in DISRUPTIVE)
                if not ok:
                    return {"status": "refused", "error": why}
            return self._effect(verb, target, params or {}, login, gate_tr,
                                context)

        # Резервирование полосы существует ДО старта нитки эффекта и
        # переживает её ответ (#569, ревью): между start() и выполнением
        # первой инструкции нитки планировщик может держать её сколь
        # угодно долго, и всё это окно полоса обязана быть занята --
        # допущенная, но ещё не исполняющаяся операция не есть свободная
        # цель. Держатель -- токен операции (объект, не нитка):
        # вызывающий занимает его до start, finally самой нитки снимает
        # по фактическому концу эффекта; неуспешный старт снимает
        # вызывающий. Слот == 1 всегда: одна операция -- один токен.
        target_key = self._lane_key(verb, target, params)
        token = self._reserve(target_key)
        try:
            drained = self._drain(target_key, ignore={token})
        except BaseException:
            # Admission упал ДО старта (fault часов в drain): эффекта
            # нет, владение нитке не передавалось -- резерв снимает тот,
            # кто его завёл. Снятие и подъём, не заглушка: причина
            # идёт наверх (#569, ревью 3).
            token.release()
            raise
        if not drained:
            token.release()
            who = target_key if target_key is not None else "the target"
            return {"status": "refused", "error":
                    f"{who}: the previous operation on this target "
                    f"is still finishing; this one did not start"}
        # Снятие держателя -- ровно два места, и оба по одну сторону
        # границы владения (#569, ревью 3): ДО успешного старта нитки
        # резерв снимает вызывающий -- неуспешный start (on_start_fail
        # в _wait_effect) и упавший admission выше; ПОСЛЕ успешного
        # старта -- ТОЛЬКО finally самой нитки эффекта по фактическому
        # концу (успех/отказ/крах/пережил срок). Ошибка наблюдателя
        # при подтверждённом старте полосу не отпускает: эффект жив,
        # его исход неизвестен, и следующая операция ждёт его в drain.
        try:
            verdict = self._limited(whole, target=target_key, token=token)
        except _TimedOut as e:
            # Срок вышел, а не эффект отказал (#497): он мог дойти до
            # цели -- исход неизвестен, повтор автоматом запрещён
            # (#476: unknown не failed). Хвост эффекта держит полосу
            # цели (#569): пока нитка жива, следующая операция этой цели
            # не начинается -- неизвестность не превращается в «свободно».
            return {"status": "unknown", "error": str(e)}
        except ValueError as e:
            # Валидация params (add/update/model/send_request) -- отказ
            # приёма, не сбой: кривой запрос не оставляет «оно сломалось».
            return {"status": "refused", "error": str(e)}
        if isinstance(verdict, dict) and verdict.get("status") in TERMINAL:
            # Готовый вердикт статуса (отказ ворот/префетча) -- как есть.
            return dict(verdict)
        try:
            if isinstance(verdict, dict) and verdict.get("outcome"):
                kind = verdict.get("outcome")
                return {"status": _FROM_OUTCOME.get(kind, "partial"),
                        "error": verdict.get("error"),
                        **{k: v for k, v in verdict.items()
                           if k not in ("outcome", "error", "progress")}}
        except Exception as e:  # noqa: BLE001 -- вердикт с причиной, не тишь
            return {"status": "failed", "error": str(e)}
        return {"status": "partial"}

    def _limited(self, effect, target=None, token=None):
        """Эффект под сроком ответа (#497, #569): вернувшемуся -- его
        результат, молчащему -- _TimedOut, которое run терминирует
        честным unknown.

        Эффект ходит в шину без своего таймаута: адресат-узел умер --
        поток стоит в bus-запросе вечно. Срок #497 возвращает run вердикт
        unknown за конечное время. Но срок отвечает за ОТВЕТ, не за жизнь
        эффекта (#569): пока нитка эффекта жива, она ещё может писать
        цель, и полоса обязана оставаться занятой ею -- прежний контракт
        «цели он больше не держит» отпускал семафор адаптера, и следующая
        операция мутировала до того, как старая закончила. Держатель
        полосы -- токен операции, занятый ДО старта нитки (окно между
        start и первой инструкцией -- тоже занятая полоса, ревью #569):
        finally самой нитки снимает его по фактическому концу -- честно
        завершившийся эффект (успех, отказ, крах) полосу освобождает
        сразу, переживший срок -- ровно до реального конца, и следующая
        операция той же цели ждёт его в _drain. Граница владения ровно
        одна (#569, ревью 3): ДО успешного старта ошибки снимает
        вызывающий (упавший admission -- run, неуспешный start --
        on_start_fail), ПОСЛЕ -- только сама нитка; ошибка наблюдателя
        при подтверждённом старте исход эффекта не определяет и полосу
        не отпускает. Нитку убить нельзя: она остаётся демоном и умирает
        с процессом. Пустой ключ (add без origin) держателя не заводит:
        полосы без имени нет."""
        if target is None:
            try:
                return self._wait_effect(effect)
            finally:
                if token is not None:
                    token.release()

        def ride():
            try:
                return effect()
            finally:
                # Фактический конец эффекта: токен снимает сама нитка --
                # ответ run (успех/таймаут/крах наблюдателя) тут ни при чём.
                token.release()

        # Ошибки ЗДЕСЬ токен не трогают: после успешного старта владение
        # у нитки, и сбой наблюдателя -- не факт о эффекте (#569, ревью 3).
        # Неудавшийся старт (start бросил до ride) -- единственный случай
        # снятия на этом пути: колбэк зовёт _wait_effect ДО того, как
        # нитка стала владельцем. Исключение самого эффекта приходит уже
        # мёртвой ниткой: ride снял токен сам, повтор был бы no-op.
        return self._wait_effect(ride, on_start_fail=token.release)

    def _lane_key(self, verb, target, params):
        """Ключ полосы операции (#569) -- effective_target: одна точка
        правила для воркера и адаптера. Пустое имя (add приходит как
        "") -- нормализация к None: иначе все targetless-операции
        делили бы одну полосу «»."""
        return effective_target(None, target or None, params)

    def _reserve(self, target_key):
        """Токен-держатель полосы цели (#569): None-ключ -- пустой токен.

        Токен -- объект операции, не нитка: занятый ДО старта нитки
        эффекта, он закрывает и окно между start и первой инструкцией
        (допущенная, но не исполняющаяся операция -- не свободная цель).
        release идемпотентен: снимают и finally самой нитки по
        фактическому концу, и вызывающий при неуспешном старте."""
        token = _LaneToken(target_key, self._lanes_lock, self._lanes)
        if target_key is not None:
            with self._lanes_lock:
                self._lanes.setdefault(target_key, set()).add(token)
        return token

    def _wait_effect(self, ride, on_start_fail=None):
        """Ждать эффекта под дедлайном ответа (#497).

        ride занимает и отпускает полосу сам: пережившее срок остаётся
        держателем, а здесь видны только эффект и срок. on_start_fail --
        колбэк неудачного СТАРТА (#569, ревью 3): зовётся только когда
        Thread.start бросил, то есть нитка-эффект не существует и
        владельца у полосы нет; ошибки ожидания после успешного старта
        (дедлайн, сбой _sleep/_now наблюдателя) колбэк не зовут -- эффект
        жив, и полоса остаётся за ним до его фактического конца."""
        box = []

        def carry():
            try:
                box.append((False, ride()))
            except Exception as e:      # тоже результат: его ждёт run
                box.append((True, e))

        worker = threading.Thread(target=carry, daemon=True,
                                  name="mop-op-effect")
        try:
            worker.start()
        except BaseException:
            # Нитки нет -- владельца нет: резерв снимает колбэк, причину
            # несём наверх без заглушки.
            if on_start_fail is not None:
                on_start_fail()
            raise
        deadline = self._now() + self.effect_timeout
        while worker.is_alive():
            if self._now() >= deadline:
                raise _TimedOut(f"effect timed out after "
                                f"{self.effect_timeout}s")
            self._sleep(0.1)
        err, value = box[0]
        if err:
            raise value
        return value

    def _drain(self, target, ignore=frozenset()):
        """Ждать конца живых хвостов цели (#569) в пределах срока эффекта.

        -> True -- полоса свободна, эффект можно начинать; False -- хвост
        пережил терпение: отказ без эффекта, с именем причины в run.
        Терпение -- срок эффекта: ответ операции остаётся ограниченным
        (терпение + собственный срок), вечного accepted нет. Хвост не
        обязан умирать к сроку: он держит полосу до фактического конца,
        следующий run повторит drain. Снявший себя держатель из слота
        исчезает. ignore -- токен своего вызова: его занятость --
        ожидаемая, а не хвост."""
        if target is None:
            return True
        deadline = self._now() + self.effect_timeout
        while True:
            with self._lanes_lock:
                slot = self._lanes.get(target)
                live = [t for t in (slot or ())
                        if t.held and t not in ignore]
            if not live:
                return True
            if self._now() >= deadline:
                return False
            self._sleep(0.1)

    def _effect(self, verb, target, params, login, transport, context=None):
        """Вызов библиотечного workflow #458: воркер не держит копий.

        Рестарт -- наблюдаемым режимом (#464): снимок берёт сама операция
        (ops.restart), наблюдение -- часть операции. Алгоритм -- в
        mop/common/ops.py, здесь вызов.

        add/update (#470): workspace -- из ЯВНОГО удалённого источника
        (self._workspace, по умолчанию remote_workspace), никогда не cwd и
        не локальный файл сервера. Update читает спеку здесь, до эффекта:
        origin прежнего репозитория решает, чей workspace качать, и в
        операцию едет один раз (не спрашивать вторично).

        Жизненный цикл (#471) -- те же общие функции puppets.recycle/
        delete/wipe, что и у CLI: обёртка кладёт их ответ в структурный
        вердикт outcome(), эффектов-копий здесь нет."""
        if verb == "add":
            origin, fields = add_request(params)
            why = self._registry_refusal(origin)
            if why:
                # Реестр ОТВЕРГ просьбу (ревью 2 #575): projectName из
                # незарегистрированного origin -- выдумка, его в аудите
                # нет; тема публикации -- нейтральная admin.
                return ops.outcome(ops.REFUSED, None, error=why,
                                   public_error=ops.PublicError("project is not registered in the pool; use mop project add"))
            target = origin
            # Проект решает реестр: он УЖЕ подтвердил origin (#575),
            # чистая функция без сети. Факт -- В МОМЕНТ подтверждения,
            # до зависания placement.
            project = ops.target_project(origin)
            if context is not None:
                context["project"] = project
            text, sent = self._source(origin)
            got = ops.add(transport, origin, (text, sent),
                          branch=params.get("branch"), context=context)
            if isinstance(got, dict):
                got.setdefault("project", project)
            return got
        if verb == "update":
            fields = update_request(params)
            spec = transport("spec", name=target) or {}
            old_origin = (spec.get("meta") or {}).get("origin")
            new_origin = (fields.get("origin")
                          if fields.get("origin") is not None else old_origin)
            text, sent = self._source(new_origin)
            return ops.update(transport, target,
                              origin=fields.get("origin"),
                              fresh=bool(fields.get("fresh")),
                              workspace=(text, sent), force=False,
                              login=login, spec=spec)
        if verb == "restart":
            return ops.restart(transport, target, login=login,
                               force=False, observe=True,
                               observe_wait=self.observe_wait,
                               sleep=lambda s: self._sleep(self.observe_poll))
        if verb == "model":
            return _wrap_model(target, login, model_request(params),
                               now=self._now,
                               sleep=lambda s: self._sleep(self.observe_poll),
                               observe_wait=self.observe_wait)
        fn = {"recycle": _wrap_recycle, "delete": _wrap_delete,
              "wipe": _wrap_wipe, "send": _wrap_send}.get(verb)
        if fn is None:
            raise RuntimeError(f"no library operation for {verb}")
        if verb == "send":
            return fn(target, login, send_request(params), context=context)
        return fn(target, login)

    def _source(self, origin):
        """Выкачка workspace (#470): подмена проверками, по умолчанию --
        manifest.fetch через remote_workspace."""
        if self._workspace is not None:
            return self._workspace(origin)
        return remote_workspace(origin)

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

    def _project_of(self, verb, target):
        """Проект цели для safety: кандидат, не финальное слово -- у send
        он вычисляется внутри эффекта, у add цели-имени нет вовсе."""
        return None
