"""Общие операции пула: add/update/restart, доставка и safe-precondition.

Две части одного слоя, знакомые по своим тикетам:

    workflow (#458) -- add/update/restart/send_to_puppet/bootstrap_result:
    командлеты держали их целиком (регистрация, ожидание узла, прогон
    bootstrap'а по метке, строки шагов); вебу (#456) нужны те же операции
    без терминала и без второй реализации -- копия разошлась бы с первой
    в тот же день.

    safety (#460) -- один инвариант безопасного действия для веб-воркера
    (safety/target_project/record): воркер шлёт глаголы субъектом проекта
    цели, и перед каждым действием обязан спросить одно и то же. Части
    независимы: safety не импортирует workflow и не зовёт его -- только
    общие домены (domain.holds_work, lease.live); workflow safety не зовёт
    сам, его вызывает воркер перед операцией.

Фронтенды выбирают своими параметрами только разрешённые себе возможности
(workspace -- локальный снимок для CLI, удалённый источник для веба), а не
копиями workflow.

Результат операции отделён от успешности побочного эффекта:
    registered  сама регистрация сошлась (джоб/аллокация есть, спека легла)
    accepted    запрос принят сервисом, итог ещё не виден (no-op, таймаут)
    refused     внятный отказ сервиса (владелец, нет джоба, шина сказала)
    partial     эффект случился, наблюдение не сошлось (регистрация без
                итога bootstrap'а по таймауту)
    unknown     ни эффекта, ни отказа мы не увидели (аллокация не поднялась
                за срок)

Прогресс -- данные ([строка, ...]), без терминальных строк и без секретов:
терминал рисует фронтенд, и ровно он же решает, что из прогресса показывать.
Вер dict-структура -- в outcome().

Контекст (#457) передаётся явно: операции не читают os.environ/cwd и не
полагаются на ContextVar потока; креды шины выбирает ctx через
context.bus_file -- кэш одного соединения процесса не знает, чей сервер.

Слой ниже client: серверный worker (#476) зовёт эти операции без
нарушения направления импортов. send_to_puppet переехал сюда из
mop/client/channel.py (#458): вердикт доставки нужен и вебу, а клиентский
модуль серверу недоступен. channel.py остаётся тонкой обёрткой для своего
слоя.

Единицы времени -- секунды; вызовы нижнего транспорта (bus) -- аргументом
transport, чтобы проверки подставляли свой и операция оставалась чистой.
"""
import time

from . import bus, busnames, context as _context
from . import puppets

# Сколько ждать подъёма аллокации (add) -- то же число, что держал командлет.
PLACE_WAIT = 120
# Итог bootstrap'а: таймаут шины плюс прогон задач (#334).
BOOTSTRAP_WAIT = busnames.BOOTSTRAP_TIMEOUT + 120


# ─── результат ───────────────────────────────────────────────────────────
# Терминальных строк и секретов в этих полях нет; пишет их операция, рисует
# фронтенд.
REGISTERED, ACCEPTED, REFUSED, PARTIAL, UNKNOWN = (
    "registered", "accepted", "refused", "partial", "unknown")


def outcome(kind, name=None, progress=(), error=None, **facts):
    """Вердикт операции одним словарём: {outcome, name, progress, error,
    факты}. Ровно эта форма -- и в CLI, и в вебе."""
    return {"outcome": kind, "name": name, "progress": list(progress),
            "error": error, **facts}


def _refused(name, error, progress=()):
    return outcome(REFUSED, name, progress, error=error)


# ─── bootstrap: общий прогон по метке (#334) ────────────────────────────
def bootstrap_result(transport, name, marker, timeout, sleep, clock, step):
    """Итог прогона СО СВОЕЙ меткой -> запись | None по таймауту.

    Итог с другой меткой -- прежний прогон, а не наш: ждём дальше.
    timeout=None -- не ждать вовсе (опрос одного текущего состояния)."""
    deadline = None if timeout is None else clock() + timeout
    while True:
        got = (transport("bootstrap_result", name=name) or {}).get("result")
        if got and got.get("sent") == marker:
            return got
        if deadline is None or clock() >= deadline:
            return None
        sleep(2)
        step("bootstrap playing")


# ─── операции ────────────────────────────────────────────────────────────
def add(transport, origin, workspace, branch=None, ctx=None,
        sleep=time.sleep, clock=time.time, step=lambda s: None,
        place_wait=PLACE_WAIT, bootstrap_wait=BOOTSTRAP_WAIT):
    """Завести папета: регистрация, ожидание узла, итог bootstrap'а.

    transport -- глаголы сервиса кластера (call_cluster-подобный),
    workspace -- (текст, происхождение) тем же видом, что собирал командлет:
    явный источник, а не чтение cwd операции. -> вердикт-словарь."""
    progress = []
    note = progress.append
    note("registering")
    text, sent = workspace
    try:
        got = transport("add", origin=origin, timeout=30, workspace=text,
                        bootstrap_sent=sent, branch=branch)
    except bus.Refused as e:
        return _refused(None, str(e), progress)
    name = got.get("name")
    if not name:
        return _refused(name, "the service named no puppet", progress)
    note(f"{name}: waiting for a node")
    deadline = clock() + place_wait
    node = None
    while clock() < deadline:
        raw = (transport("alloc", name=name) or {}).get("alloc")
        a = puppets.Alloc.from_dict(raw)
        if a:
            node = a.node
            if a.running:
                return _bootstrap(transport, name, got, progress,
                                  bootstrap_wait, sleep, clock, step)
            if a.client_status == "failed" or puppets.failing(raw):
                why = puppets.failing(raw) or "failed to start"
                return outcome(UNKNOWN, name, progress,
                               error=f"{name} on {node}: {why}")
            note(f"{name}: starting on {node}")
        sleep(1)
    return outcome(UNKNOWN, name, progress,
                   error=f"{name} is "
                         + (f"still starting on {node}" if node
                            else "not placed on any node")
                         + " after 2 minutes: mop server doctor")


def _bootstrap(transport, name, got, progress, wait, sleep, clock, step):
    """Прогон bootstrap'а своей регистрации -> итог операции."""
    marker = got.get("bootstrap_marker")
    if not marker:
        # Старый сервер или клиент без происхождения: тишина и есть итог.
        return outcome(REGISTERED, name, progress)
    if got.get("replaced") is False:
        return outcome(ACCEPTED, name, progress, replaced=False)
    record = bootstrap_result(transport, name, marker, wait, sleep, clock, step)
    if record is None:
        return outcome(PARTIAL, name, progress,
                       marker=marker, bootstrap=None, waited=wait)
    # Прогона не было (played False) -- не провал: новый прогон просто не
    # состоялся, и это не отказ сервиса.
    if not record.get("played"):
        return outcome(ACCEPTED, name, progress,
                       marker=marker, bootstrap=record)
    ok = bool(record.get("ok"))
    return outcome(REGISTERED if ok else REFUSED, name, progress,
                   marker=marker, bootstrap=record,
                   error=None if ok else _bootstrap_error(record))


def _bootstrap_error(record):
    """Строка провала прогона -- та же норма, что печатал командлет (#334)."""
    if record.get("task"):
        line = f"bootstrap failed at task «{record['task']}»"
        return line + (f": {record['message']}" if record.get("message") else "")
    line = "bootstrap failed"
    if record.get("rc") is not None:
        line += f" (rc {record['rc']})"
    return line + (f": {record['last']}" if record.get("last") else "")


def update(transport, name, origin=None, fresh=False, workspace=None,
           branch=None, force=False, login=None, ctx=None, spec=None,
           sleep=time.sleep, clock=time.time, step=lambda s: None,
           bootstrap_wait=BOOTSTRAP_WAIT):
    """Перевести папета: смена origin, fresh/resume, перерегистрация.

    origin=None -- прежний репозиторий. fresh при смене origin вынужден:
    разговор прежнего проекта в чужом репозитории -- чужой контекст.
    no-op (спека не изменилась) -- accepted, не registered: эффект
    «принято», нового прогона не будет. spec -- уже прочитанная спека
    (перила мастер-шелла отдали): не спрашивать вторично."""
    spec = spec if spec is not None else transport("spec", name=name)
    if spec.get("error"):
        return _refused(name, spec["error"])
    meta = puppets.JobMeta.from_meta(spec.get("meta"))
    old = meta.origin
    if not old:
        return _refused(name, "no origin in the spec — not a pool puppet")
    new_origin = origin or old
    cont = not fresh and new_origin == old
    text, sent = workspace or ("", None)
    try:
        got = transport("update", name=name, origin=new_origin,
                        cont=cont, new_origin=new_origin if new_origin != old else None,
                        workspace=text, bootstrap_sent=sent,
                        owner=login, force=force, branch=branch)
    except bus.Refused as e:
        return _refused(name, str(e))
    if got.get("error"):
        return _refused(name, got["error"])
    if got.get("bootstrap_marker") is None:
        # Спека не изменилась: Nomad не переразмещает (#334).
        return outcome(ACCEPTED, name, replaced=got.get("replaced"),
                       owner_note=got.get("owner_note"))
    return _bootstrap(transport, name, got, [], bootstrap_wait, sleep, clock, step)


def restart(transport, name, force=False, login=None, observe=False,
            before=None, observe_wait=30, sleep=time.sleep, clock=time.time):
    """Перезапустить сессию папета на том же клоне. -> вердикт-словарь.

    Отказ владельца -- refused с именем владельца в error; успех без
    наблюдения итога -- accepted: рестарт не имеет своего прогона.

    observe -- явный режим (#464): тот же глагол restart, но операция
    подтверждает рестарт задачи реальным сигналом, а не «accepted на
    слово». Сигнал -- сводка задачи глагола alloc (read-only, едет с #126):
    снимок ДО RPC, затем опрос, пока счётчик рестартов не сдвинется или
    задача не выйдет из running (перезапуск или ожидание его). Прежний
    running -- не успех: это та же старая сессия. Смена allocation ID --
    НЕ подтверждение (рестарт задачи, а не новая аллокация): это unknown.
    Увиденный рестарт с вернувшейся падшей задачей -- partial с причиной;
    увиденный без вернувшегося running за срок -- partial. Итог ---
    registered (снова running), partial, refused, unknown. CLI без observe
    -- прежний контракт байт в байт; force наблюдение не выключает.
    before -- уже снятый снимок аллокации (его берёт воркер #476, записав
    базу в журнал ДО эффекта); без него снимок берёт сама операция."""
    if not observe:
        return _restart_plain(transport, name, force, login)
    if before is None:
        try:
            before = task_snapshot(transport, name)
        except Exception:
            before = None
    try:
        got = transport("restart", name=name, owner=login, force=force)
    except bus.Refused as e:
        return _refused(name, str(e))
    if got.get("error"):
        return _refused(name, got["error"])
    return _observe(transport, name, before, observe_wait, sleep, clock)


def observe_restart(transport, name, before, observe_wait=0,
                    sleep=lambda s: None, clock=time.time):
    """Донаблюдать рестарт задачи (#464) тем же сигналом, не переигрывая
    эффект: восстановление воркера #476 для прерванного наблюдаемого
    рестарта. Алгоритм один -- тот же _observe; вердикт -- outcome()."""
    return _observe(transport, name, before, observe_wait, sleep, clock)


def task_snapshot(transport, name):
    """Снимок аллокации ДО рестарта; None, если сводки задачи в ответе нет."""
    raw = (transport("alloc", name=name) or {}).get("alloc") or {}
    return raw if raw.get("task") else None


def _restart_plain(transport, name, force, login):
    """Прежний контракт: RPC, вердикт по ответу, без наблюдения."""
    try:
        got = transport("restart", name=name, owner=login, force=force)
    except bus.Refused as e:
        return _refused(name, str(e))
    if got.get("error"):
        return _refused(name, got["error"])
    return outcome(ACCEPTED, name, owner_note=got.get("owner_note"))


def _observe(transport, name, before, wait, sleep, clock):
    """Дождаться сигнала рестарта задачи и факта работоспособности.

    before -- снимок аллокации ДО RPC. Сигнал -- сдвиг счётчика рестартов
    или выход задачи из running (перезапуск либо ожидание его); потом --
    снова running, не failed. Смена allocation ID подтверждением не
    служит (тикет #464: рестарт задачи, а не новая аллокация) -- unknown."""
    def snap():
        return (transport("alloc", name=name) or {}).get("alloc") or {}

    def mistake(raw, error):
        task = (raw or {}).get("task") or {}
        return outcome(UNKNOWN, name, error=error,
                       alloc=(raw or {}).get("ID"),
                       restarts=task.get("restarts"))

    deadline = clock() + wait
    base_restarts = ((before or {}).get("task") or {}).get("restarts") or 0
    base_id = (before or {}).get("ID")
    # Фаза 1: сигнал. Опрос read-only до сдвига счётчика, выхода задачи
    # из running или срока.
    while True:
        try:
            raw = snap()
        except Exception as e:
            return mistake(None, f"{name}: the restart is not observed: {e}")
        task = raw.get("task")
        aid = raw.get("ID")
        if base_id and aid and aid != base_id:
            # Аллокацию заменили -- рестарт задачи не подтверждён.
            return mistake(raw, f"{name}: the allocation was replaced "
                                f"({base_id} -> {aid}), not restarted")
        if task is None:
            return mistake(raw, f"{name}: no task to observe "
                                f"(the allocation has no task states)")
        moved = (task.get("restarts") or 0) > base_restarts
        left_running = bool(task.get("state")) \
            and task["state"].lower() != "running"
        if moved or left_running:
            break
        if clock() >= deadline:
            return mistake(raw, f"{name}: no restart seen in {wait}s "
                                f"(the old task kept running)")
        sleep(2)
    # Фаза 2: факт работоспособности -- задача снова running, не failed.
    while True:
        try:
            raw = snap()
        except Exception as e:
            return mistake(None, f"{name}: the restarted task is not "
                                 f"confirmed: {e}")
        aid = raw.get("ID")
        task = raw.get("task")
        if base_id and aid and aid != base_id:
            # id сменился уже после сигнала: подтверждение потеряло силу.
            return mistake(raw, f"{name}: the allocation was replaced "
                                f"({base_id} -> {aid}), not restarted")
        if task is None:
            return mistake(raw, f"{name}: no task to confirm")
        if task.get("failed"):
            why = f"{name}: the task failed after the restart"
            if raw.get("reason"):
                why += f": {raw['reason']}"      # причина падения (#126)
            elif task.get("exit") is not None:
                why += f" (exit {task['exit']})"
            return outcome(PARTIAL, name, alloc=aid,
                           restarts=task.get("restarts"), error=why)
        if (task.get("state") or "").lower() == "running":
            return outcome(REGISTERED, name, alloc=aid,
                           restarts=task.get("restarts"))
        if clock() >= deadline:
            return outcome(PARTIAL, name, alloc=aid,
                           restarts=task.get("restarts"),
                           error=f"{name}: restarted but not running "
                                 f"again within {wait}s")
        sleep(2)


# ─── доставка (#458: из mop/client/channel.py) ──────────────────────────
def send_to_puppet(transport, node, name, message, priority="next", wait=0,
                   notify=None, **fields):
    """Доставка через агента узла. -> вердикт | bus.BusError.

    transport -- глагол агенту узла (bus.request-подобный). Слой client
    остаётся тонкой обёрткой над этим же телом; потолок ожидания --
    channel.MAX_WAIT у вызывающего, здесь -- clamp самого значения."""
    wait = _clamp_wait(wait)
    extra = {} if notify is None else {"notify": bool(notify and not wait)}
    r = transport(node, "send", name=name, message=message, priority=priority,
                  wait=wait, **extra, **fields, timeout=wait + bus.TIMEOUT)
    v = {"kind": "puppet", "to": name, "wait": wait,
         "notify": extra.get("notify", False)}
    if "error" in r:
        return dict(v, error=r["error"])
    return dict(v, msg_id=r.get("msg_id"), owner_note=r.get("owner_note"),
                idle=r.get("idle"))


def _clamp_wait(seconds, ceiling=600):
    """Сколько ждать простоя: 0..ceiling. Та же норма, что channel."""
    return min(max(seconds or 0, 0), ceiling)


# ─── safe-precondition веб-мутаций (#460) ────────────────────────────────
# Воркер дашборда шлёт изменяющие глаголы как машина под субъектом проекта
# цели: ворота владения сервиса (#40, lease.gate) начинают работать как для
# мастера. Но ворота пускают СВОЕГО -- а «свой» для веб-клика решает не
# тело запроса, а HTTP-сессия человека. Ниже -- один инвариант на все
# handlers воркера, копий по тикетам нет. Часть независима от workflow
# выше: только общие домены (domain.holds_work, lease.live).
from .domain import CloneFacts, holds_work
from . import lease


def target_project(origin, job_project=None):
    """Проект цели -- из origin джоба (Meta.origin), не из имени.

    Один источник права: origin берёт воркер из джоба, имя проекта --
    puppets.project_of(origin). Префикс имени папета и тело запроса здесь
    не участвуют: их пишет кто угодно. job_project -- зарегистрированный
    проект (реестр сервера) для add, где джоба ещё нет: регистрируемый
    origin обязан быть в реестре, и его проект говорит реестр."""
    if origin is None:
        return job_project
    return puppets.project_of(origin)


def safety(name, project, owner=None, facts=None, busy=False,
           disruptive=False, now=None):
    """Безопасно ли действие воркера над папетом. -> (можно, причина).

    Один инвариант (#460):
      - факты клона известны: ошибка агента, тишина (None) и битый ответ --
        отказ («не знаю» не значит «пусто», AGENT SILENT -- папет может
        работать);
      - работы в клоне нет -- включая ничейную: веб-клик не диспатч, окно
        lease.WINDOW ему не положено (это дорога CLI-мастера);
      - чужой живой аренды нет; своя -- проходит;
      - disruptive (рестарт, stop, wipe) при живой занятой сессии цели --
        отказ даже со своей арендой: сессию ведёт человек.

    owner -- логин человека из HTTP-сессии: для него «своя» аренда.
    busy -- жива ли сессия цели (вердикт ростера). facts -- ответ _clone_of:
    {"clone": ...} | {"error": ...} | None. Окно между проверкой и эффектом
    не заперто -- как у CLI."""
    if now is None:
        now = time.time()
    if facts is None or facts.get("error") or not facts.get("clone"):
        return False, (f"{name}: cannot tell if it holds work — "
                       f"{(facts or {}).get('error') or 'no facts from the node'}")
    clone = CloneFacts.from_dict(facts["clone"])
    if holds_work(clone):
        whose = clone.owner.user if clone.owner else "nobody"
        return False, (f"{name}: work in the clone (led by {whose}) — "
                       f"the web does not take work over")
    if clone.owner and clone.owner.user != owner:
        if lease.live(clone.owner, clone, now):
            return False, (f"{name}: led by {clone.owner.user} — "
                           f"ask them, the web does not take leases over")
    if disruptive and busy:
        return False, (f"{name}: the session is live — a disruptive action "
                       f"would kill the work in it")
    return True, None


def record(verb, name, project, principal, sender="mop-web"):
    """Аудит-запись операции воркера (#476): принципал HTTP и сервис --
    отдельные поля, payload сообщения -- НЕ входит (ключи не хранятся).

    owner_note и события шины сохраняются воркером отдельно; здесь только
    сама запись: кто из людей велел, какой сервис исполнил, что и над кем."""
    return {"event": "op", "verb": verb, "name": name, "project": project,
            "principal": principal, "sender": sender}
