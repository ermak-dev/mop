"""Общие операции пула: add/update/restart и доставка (#458).

Командлеты держали workflow целиком: регистрация, ожидание узла, прогон
bootstrap'а по метке, строки шагов. Вебу (#456) нужны те же операции, но
без терминала и без второй реализации: копия разошлась бы с первой в тот
же день. Слой один, общие для CLI и веба; фронтенды выбирают своими
параметрами только разрешённые себе возможности (workspace -- локальный
снимок для CLI, удалённый источник для веба), а не копиями workflow.

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
слоя. Безопасность действий -- не здесь: safe-precondition один в #460
(ops.safety), этот слой его только вызовет.

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
                return outcome(UNKNOWN, name, progress,
                               error=puppets.failing(raw) or "failed to start")
            note(f"{name}: starting on {node}")
        sleep(1)
    return outcome(UNKNOWN, name, progress,
                   error="not placed on any node in time")


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
                       marker=marker, bootstrap=None)
    ok = bool(record.get("played") and record.get("ok"))
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
           branch=None, force=False, login=None, ctx=None,
           sleep=time.sleep, clock=time.time, step=lambda s: None,
           bootstrap_wait=BOOTSTRAP_WAIT):
    """Перевести папета: смена origin, fresh/resume, перерегистрация.

    origin=None -- прежний репозиторий. fresh при смене origin вынужден:
    разговор прежнего проекта в чужом репозитории -- чужой контекст.
    no-op (спека не изменилась) -- accepted, не registered: эффект
    «принято», нового прогона не будет."""
    spec = transport("spec", name=name)
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
        return outcome(ACCEPTED, name, replaced=got.get("replaced"))
    return _bootstrap(transport, name, got, [], bootstrap_wait, sleep, clock, step)


def restart(transport, name, force=False, login=None):
    """Перезапустить сессию папета на том же клоне. -> вердикт-словарь.

    Отказ владельца -- refused с именем владельца в error; успех без
    наблюдения итога -- accepted: рестарт не имеет своего прогона."""
    try:
        got = transport("restart", name=name, owner=login, force=force)
    except bus.Refused as e:
        return _refused(name, str(e))
    if got.get("error"):
        return _refused(name, got["error"])
    return outcome(ACCEPTED, name, owner_note=got.get("owner_note"))


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
