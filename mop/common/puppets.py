"""Папеты пула глазами мастера: ростер через шину, диагноз и лечение, рецикл,
ввод в TUI папета. Спека джоба — mop/server/spec.py, Nomad-половина ростера — сервис
кластера (mop/server/cluster.py), вердикт о состоянии — mop/common/state.py (#145, #152).

Модуль возвращает данные и ничего не печатает. Форматирование живёт во
фронтендах (командлеты в bin/ печатают таблицы, mop mcp отдаёт то же самое
модели) — иначе второй фронтенд неизбежно начал бы разбирать чужой текст.
"""
import os
import time

from . import bus, config, lease, state
from .. import driver
from .domain import Alloc, CloneFacts, JobMeta, NodeRow, PoolNode, holds_work
from .state import PuppetRow, State, action_for, failing_row, silent, spec_action, verdict

PROJECT = config.PROJECT

# Соглашение об имени папета (префикс, разбор, каталоги) живёт в реестре
# драйверов: это единственный stdlib-модуль, который читают и мастер, и узел.
# Здесь — только имена, под которыми его знает мастер.
JOB_PREFIX = driver.PREFIX
clone_dir = driver.clone_dir

# ─── LLM-профили ─────────────────────────────────────────────────────────
# Папет — всегда claude code; профиль меняет ровно одно: куда он ходит за
# токенами. Anthropic-совместимый эндпоинт провайдера (ANTHROPIC_BASE_URL),
# ключ (ANTHROPIC_AUTH_TOKEN) и карта имён моделей opus/sonnet/haiku в модели
# провайдера — больше в папет ничего не меняется, поэтому tmux, tail, doctor
# и детект залипаний работают одинаково для любого профиля.
#
# Сами профили — плагины в mop/common/llm/ (один файл = один профиль, имя файла =
# имя). Здесь только потребление. Дефолт — настройка установки: контора на
# одном провайдере меняет дефолт, а не каждую команду.
# Источник правды по секретам проекта — .env рядом с кодом. Всё, что задаёт
# человек, лежит там; порождаемое само (пароли NATS, токен Nomad, логин
# claude.ai) — не там и туда не попадает.
LOCAL_KEYS_FILE = config.ENV_FILE   # тот же файл, одно определение (#217)


# Правило проекта -- у драйвера (#154): агент тоже его знает, а puppets
# импортировать не может.
project_of = driver.project_of


def looks_like_origin(text):
    """Похоже ли на git-origin: есть хост или путь (`:` или `/`).

    Голое имя проекта origin'ом не является -- обратного отображения «имя ->
    origin» в системе нет. И значение чужого флага, приехавшее позиционно
    (`mop master --model opus` -> `opus`), тоже: без этой проверки дальше
    было бы «нет проекта opus», и опечатку искали бы не там (#111)."""
    return driver.parse_origin(text) is not None


def project_ids(lines):
    """Строки памяти/аргументов -> ({origin'ы}, {легаси-имена}).

    Память проектов хранит ORIGIN'ы, а не имена (#33): имя выводится
    basename'ом, а вот имя в origin разворачивать некуда — таблицы имён
    нет и заводить нельзя. Строка без / и : — имя с легаси-времён, origin
    которого уже не узнать; такие не теряются, иначе их проект молча
    выпадает из конфига NATS при следующем deploy.
    """
    stripped = {l.strip() for l in lines if l.strip()}
    origins = {l for l in stripped if looks_like_origin(l)}
    return origins, stripped - origins


def visible(listing, project):
    """Папеты из сырого списка джобов глазами одного проекта (#29).

    Папет — service-джоб с префиксом pu-; pu-cleanup и его периодические
    дети — sysbatch, им в ростере места нет. Origin в Meta у папета может
    не быть: это спека старой регистрации, никогда не перерегистрированная
    (врапер живёт в спеке), и такой папет работает, но невидим — на этом
    пул rugent «исчезал» из всех списков, оставаясь живым. Псевдопроект admin
    видит и непомеченных; проект — только помеченных своим origin: чей
    непомеченный, из него самого не узнать."""
    project = project or bus.PROJECT
    out = []
    for j in listing:
        if not j.get("ID", "").startswith(JOB_PREFIX) or j.get("Type") != "service":
            continue
        origin = JobMeta.from_job(j).origin
        if project == bus.ADMIN:
            out.append(j)
        elif origin and project_of(origin) == project:
            out.append(j)
    return out


def _cluster(verb, project=None, timeout=bus.TIMEOUT, **fields):
    """Глагол сервису кластера с громким отказом. -> ответ.

    Ошибка сервиса — это исключение здесь, а не поле в ответе: вызывающие в
    этом модуле — библиотечные функции, и молча вернуть половину ответа
    значит показать половину пула как целый."""
    return bus.call_cluster(verb, project=project, timeout=timeout, **fields)


def items(project=None, stale=False):
    """Ростер через шину: джобы и аллокации от сервиса кластера.

    Мастер больше не ходит в Nomad — ни ростером, ни чем-либо ещё (#81).
    Видимость считает сервис тем же visible(), но по проекту из СУБЪЕКТА:
    расширить её, подставив чужой проект, нельзя — права NATS не дадут."""
    return _cluster("roster", project=project, stale=stale).get("items") or []


def jobs(project=None):
    """Джобы папетов, видимые этому процессу: `mop master` ставит MOP_PROJECT,
    и мастер проекта перестаёт видеть чужих папетов уже здесь, в ростере.
    Кто виден кому — visible(): #29."""
    return [i["job"] for i in items(project)]


# ─── сводки для фронтендов ───────────────────────────────────────────────
def failing(alloc):
    """Строка состояния падающего папета из ответа `alloc`, либо None (#126)."""
    a = Alloc.from_dict(alloc) or Alloc()
    row = failing_row(a.client_status, a.task, a.reason)
    return row[1] if row else None


def not_running(name, alloc):
    """Почему к папету нельзя подключиться: падает (с причиной) или не
    запущен. alloc -- из ответа `alloc` сервиса кластера либо None."""
    why = failing(alloc)
    return f"{name}: {why}" if why else f"{name} not running"


def running(name):
    """Работающая аллокация папета и драйвер её узла, одним запросом.
    -> (alloc, драйвер) | LookupError | bus.Refused.

    Одна на командлеты и MCP (#146): у MCP была своя копия, и отказ сервиса
    («не твой папет») она читала как «не размещён» -- модель видела ошибку
    без причины. Отказ сервиса едет Refused как есть, неработающий папет --
    LookupError, у падающего с причиной падения."""
    got = _cluster("alloc", name=name)
    a = got.get("alloc")
    if got.get("gave_up"):
        # Сдавшийся bootstrap (#345): джоб остановлен, причина -- у сервера.
        raise LookupError(f"{name}: {State('failing', got['gave_up'])}")
    if not (Alloc.from_dict(a) or Alloc()).running:
        raise LookupError(not_running(name, a))
    return a, got.get("driver")


def running_alloc(name):
    """Работающая аллокация папета (см. running)."""
    return running(name)[0]


def node_of(name, running=True):
    """Узел папета -- одна на CLI и канал (#377): четыре командлета считали
    его каждый по-своему. running -- узел работающего папета (tail, send,
    channel: LookupError, если не бежит); иначе -- узел последней аллокации
    в любом статусе (wipe: сносят как раз остановленного)."""
    if running:
        return Alloc.from_dict(running_alloc(name)).node
    alloc = Alloc.from_dict(_cluster("alloc", name=name).get("alloc"))
    if not alloc:
        raise LookupError(f"{name}: no allocation — node unknown")
    return alloc.node


def roster(stale=False):
    """Ростер пула плюс состояние с узлов, одним заходом.

    Общая часть list и doctor. Раньше каждый ходил на узлы сам и платил по
    четыре рукопожатия exec'а за папета — на десяти папетах это сорок
    последовательных подключений, и столько же ещё раз, если следом звали
    doctor. Теперь: один запрос в Nomad за ростером и по одному запросу на
    узел за всеми его папетами, параллельно по одному соединению.

    Места здесь нет намеренно: обмер диска стоит секунд, а состояние приходит
    за десятые доли. Кому место нужно, тот берёт puppet_rows_stream; doctor
    ждал du впустую, ни разу в него не заглянув.

    -> [{job, alloc, state, kind}]: state — строка для показа, kind — вид
    вердикта, на котором стоят решения (#145); оба None там, где спрашивать
    некого.
    """
    got, by_node = [], {}
    for item in items(stale=stale):
        item["state"] = item["kind"] = None
        item["owner"] = None
        alloc = Alloc.from_dict(item["alloc"])
        if alloc and alloc.running:
            by_node.setdefault(alloc.node, []).append(item)
        got.append(item)

    # Шина легла целиком — ростер всё равно показываем. Он приходит из Nomad и
    # к шине отношения не имеет; уронить `list` вместе с ней значит оставить
    # мастера без единственной картины пула ровно тогда, когда что-то сломалось.
    try:
        answers = bus.request_many("states", {
            node: {"names": [i["job"]["ID"] for i in its]}
            for node, its in by_node.items()})
    except bus.BusError as e:
        answers = {node: bus.BusError(str(e)) for node in by_node}

    for node, its in by_node.items():
        answer = answers.get(node)
        # Молчащий агент — отдельная болезнь, не "папет завис": папет при этом
        # может прекрасно работать, и рестартить его нельзя.
        if isinstance(answer, Exception):
            for i in its:
                _judge(i, silent(answer))
            continue
        seen = (answer or {}).get("puppets") or {}
        now = time.time()
        for i in its:
            f = seen.get(i["job"]["ID"])
            _judge(i, verdict(f))
            i["owner"] = owner_of(f, now)
    return got


def _judge(item, state):
    """Вердикт в строку ростера: строка — показать, вид — решать по нему."""
    item["state"], item["kind"] = str(state), state.kind


def owner_of(facts, now):
    """Кто ведёт задание папета (#161), если аренда живая; иначе None."""
    clone = CloneFacts.from_dict((facts or {}).get("clone"))
    owner = clone and clone.owner
    return owner.user if lease.live(owner, clone, now) else None


def rows_from(items):
    """Строки без обмера из ростера."""
    return [_row(i) for i in items]


def _row(item, disk_kb=None):
    job, alloc = item["job"], Alloc.from_dict(item["alloc"])
    meta = JobMeta.from_job(job)
    status = item["error"] or (alloc.client_status if alloc else job.get("Status", "?"))
    state, kind = item["state"] or None, item.get("kind")
    # Падающий на старте -- failing с причиной, а не pending (#126).
    failing = failing_row(status, item.get("task"), item.get("reason"))
    if failing and not item["error"]:
        status, state = failing
        kind = "failing"
    # Сдавшийся bootstrap (#345): джоб остановлен сервером, причина -- из
    # записи итога, а не из stderr (аллокации может не быть вовсе) и без
    # хвоста «(N restarts, …)»: считал не Nomad, а сервер.
    if item.get("gave_up") and not item["error"]:
        status, state, kind = "failed", str(State("failing", item["gave_up"])), "failing"
    return PuppetRow(
        name=job["ID"],
        node=alloc.node if alloc else None,
        alloc_status=status,
        state=state,
        kind=kind,
        owner=item.get("owner") or None,
        # Нет ключа в мете -- None; «?» рисует показ (PuppetRow.render, #274).
        origin=meta.origin,
        disk_kb=disk_kb,
    )


# Обмер места на узлах (sizes): и потоком ростера, и отдельным обмером (#268).
SIZES_TIMEOUT = 45


def puppet_rows_stream():
    """Папета как данные, но строки отдаются по мере готовности.

    Ждать нечего только на бумаге: состояния всего пула приходят за десятые
    доли секунды, а обмер места — секунды, и печатать нечего, пока не сойдётся
    весь обмер. Поэтому место спрашивается по одному папету и строка уходит
    наружу, как только сошлась её собственная.

    Отсюда и порядок — по готовности, а не по имени. Кому нужен стабильный
    (таблица MCP, doctor), тот зовёт puppet_rows, который просто сортирует.

    Обмер живёт отдельным поездом со щедрым таймаутом: вплавить его в states
    значило бы читать медленный du как «агент молчит». Не доехало — прочерк в
    колонке, а состояние на месте.
    """
    items = roster()
    asked, rest = {}, []
    for i in items:
        alloc = Alloc.from_dict(i["alloc"])
        if alloc and alloc.running:
            asked[i["job"]["ID"]] = (alloc.node, {"names": [i["job"]["ID"]]})
        else:
            rest.append(i)
    by_name = {i["job"]["ID"]: i for i in items}

    # Папета, у которых спрашивать некого, ждать нечего — они уходят первыми.
    for i in rest:
        yield _row(i)
    try:
        for name, answer in bus.request_stream("sizes", asked, timeout=SIZES_TIMEOUT):
            sizes = ({} if isinstance(answer, Exception)
                     else ((answer or {}).get("sizes") or {}))
            yield _row(by_name[name], sizes.get(name))
    except bus.BusError:
        # Шина легла целиком — ростер всё равно показываем: он из Nomad и к
        # шине отношения не имеет.
        for name in asked:
            yield _row(by_name[name])


def puppet_rows(sizes=True):
    """Папета как данные: [PuppetRow] (#204); kind — вид вердикта
    (mop/common/state.py), state — его строка. disk_kb — клон плюс target,
    обмеряется спросом; None — du не доехал, это прочерк, а не ноль.

    sizes=False — без обмера вовсе: дашборд (mop/server/web.py) опрашивает
    состояния в разы чаще, чем место, и ждать du на каждом круге значило бы
    показывать состояние с опозданием на обмер. Место он берёт отдельно,
    puppet_sizes, своим расписанием."""
    if not sizes:
        return sorted(rows_from(roster()), key=lambda r: r.name)
    return sorted(puppet_rows_stream(), key=lambda r: r.name)


def puppet_sizes(rows, timeout=SIZES_TIMEOUT):
    """Обмер места по строкам puppet_rows: {имя: КБ}. Спрашиваются только
    те, у кого бежит аллокация; кого не обмерили — в ответе нет, и это
    прочерк у вызывающего. Легла шина — пустой ответ, не исключение: место
    здесь не главное, а состояние уже показано."""
    asked = {}
    for r in rows:
        if r.alloc_status == "running" and r.node is not None:
            asked[r.name] = (r.node, {"names": [r.name]})
    out = {}
    try:
        for name, answer in bus.request_stream("sizes", asked, timeout=timeout):
            if isinstance(answer, Exception):
                continue
            kb = ((answer or {}).get("sizes") or {}).get(name)
            if kb is not None:
                out[name] = kb
    except bus.BusError:
        pass
    return out


def pool():
    """Ёмкость пула через шину:
    [{name, status, free_mb, total_mb, slots, slots_total}].

    Мастеру это видно и должно быть видно: по свободным слотам он решает,
    заводить ли ещё папета. Кто ещё живёт на узле и чьи образы там собраны —
    не видно: это `mop node`, глагол оператора (docs/CLUSTER.md).

    Отказ сервиса -- bus.Refused с его текстом, а не пустой пул (#163):
    пустота читалась как «узлов нет», и по ней работало всё, что идёт через
    ready_nodes."""
    return [PoolNode.from_pool(n) for n in bus.call_cluster("pool").get("nodes") or []]


def nodes():
    """Узлы пула через шину -- для всех, кроме сервера. Глагол оператора:
    строка узла говорит, чьи образы на нём собраны и кто его делит.
    Отказ сервиса -- Refused, а не пустая таблица (#163)."""
    return [NodeRow.from_row(n) for n in bus.call_cluster("nodes").get("nodes") or []]


def ready_nodes():
    """Имена узлов, на которые Nomad вообще станет что-то ставить. Через шину:
    тот же ответ, что раньше давал nomad.ready_nodes() на машине оператора."""
    return {n.name for n in pool() if n.placeable}


# ─── диагностика ─────────────────────────────────────────────────────────
# Категории и лечение:
#   залип/не отвечает      -> restart аллокации (клон и ветка переживают)
#   not logged in/expired  -> раздать креды на пул, затем restart папета
#   pending/failed/lost    -> alloc stop: Nomad пересоздаёт сразу, минуя
#                             restart-backoff (до 30 мин)
#   no model quota/error   -> печать /model в пейн; рестарт квоту не вернёт
#   queued без аллокации   -> мест в пуле нет, лечится не отсюда
#   queued, узла нет       -> образа проекта нет ни на одном узле: mop project add
#   агент узла молчит      -> отсюда никак: лечится юнитом на самом узле


def diagnose():
    """Проблемы пула как данные: [{name, alloc, diagnosis, action}]."""
    issues = []
    for item in roster(stale=True):
        job, alloc = item["job"], item["alloc"]
        # Сдавшийся bootstrap (#345): стоп -- решение сервера, лечится правкой
        # .mop/bootstrap.yaml и `mop update`; автолечения нет. Раньше вердикта
        # о спеке: перерегистрация по кругу doctor'а дала бы тот же провал.
        if item.get("gave_up"):
            issues.append({"name": job["ID"], "alloc": alloc, "action": None,
                           "diagnosis": str(State("failing", item["gave_up"]))})
            continue
        # Спека проверяется раньше состояния: папет со старой спекой может
        # выглядеть совершенно здоровым ровно до первого перепланирования.
        # Вердикт считает сервис кластера: спека лежит в Nomad, а сюда её
        # больше не возят.
        #
        # Перерегистрация — только свободному (#174): после раскатки нового
        # шаблона устаревшим читается весь пул сразу, и --fix не вправе
        # перезапустить ни одной занятой сессии. Занятый показан и проходит
        # остальные проверки: залипшему нужен его restart и сейчас.
        if item.get("stale"):
            action = spec_action(item.get("kind"))
            if action:
                issues.append({"name": job["ID"], "alloc": alloc,
                               "diagnosis": "job spec is from an older mop",
                               "action": action})
                continue
            issues.append({"name": job["ID"], "alloc": alloc, "action": None,
                           "diagnosis": f"job spec is from an older mop; not "
                                        f"re-registered while {item['state']}"})
        # Падает на старте (#126): снять аллокацию -- только начать тот же круг
        # заново; лечится причина, поэтому без автолечения и с ней в диагнозе.
        status = Alloc.from_dict(alloc)
        failing = failing_row(status and status.client_status, item.get("task"),
                              item.get("reason"))
        if failing:
            issues.append({"name": job["ID"], "alloc": alloc,
                           "diagnosis": failing[1], "action": None})
            continue
        if not alloc or status.client_status in ("lost", "unknown", "failed", "pending"):
            issues.append(_placement_issue(job, alloc, item.get("unserved"),
                                           item.get("ceiling")))
            continue
        action = action_for(item["kind"])
        if action is not False:
            issues.append({"name": job["ID"], "alloc": alloc,
                           "diagnosis": item["state"], "action": action})
    return issues


def _placement_issue(job, alloc, unserved=False, ceiling=None):
    """Диагноз джоба, который не стоит. unserved -- пометка сервиса
    кластера (spec.placement_gap): ни один готовый узел не обслуживает
    проект (#118) или, со значением "memory", потолок каждого из них ниже
    ceiling, потолка папета (#197)."""
    name = job["ID"]
    a = Alloc.from_dict(alloc)
    if a and a.client_status in ("pending", "failed"):
        return {"name": name, "alloc": alloc, "action": state.STOP,
                "diagnosis": f"allocation {a.client_status} (restart-backoff?)"}
    if a:
        return {"name": name, "alloc": alloc, "action": state.STOP,
                "diagnosis": f"allocation {a.client_status}"}
    if state.queued(job) and unserved == "memory":
        # Просьба проекта больше, чем готова дать любая машина с его образом:
        # ни ожидание, ни сборка образа не помогут.
        project = JobMeta.from_job(job).project
        return {"name": name, "alloc": None, "action": None,
                "diagnosis": f"queued — no ready node of {project} takes a "
                             f"{ceiling} MB puppet (node meta mop_mem_cap_mb): "
                             f"lower MOP_MEM_MB in {project}'s .mop or raise "
                             f"mop_body_mem_cap_mb of a node"}
    if state.queued(job) and unserved:
        # Слоты тут ни при чём: ограничение размещения по образу (#10) не
        # пускает никуда, и ожидание не вылечит ничего.
        origin = JobMeta.from_job(job).origin or "<origin>"
        return {"name": name, "alloc": None, "action": None,
                "diagnosis": f"queued — no ready node has an image of "
                             f"{project_of(origin)}: mop project add {origin}"}
    if state.queued(job):
        return {"name": name, "alloc": None, "action": None,
                "diagnosis": "queued — no free slots in the pool"}
    return {"name": name, "alloc": None, "action": None, "diagnosis": "no allocation"}


# Чем будят папета после раздачи кредов (#290): слово оператора, ход
# продолжается с места обрыва. Не «resume»: так папет читал бы это как
# просьбу восстановить сессию.
NUDGE = "продолжай"


def treat(issue):
    """Применить лечение к одной проблеме из diagnose. -> что вышло, строкой.

    Одно место для `mop server doctor --fix` и других вызовов лечения:
    пока лечение жило в каждом своём, они разошлись — CLI перерегистрировал
    спеку по диагнозу `update`, а MCP на тот же диагноз делал рестарт, то есть
    поднимал ту же старую спеку (#47). Гейт «креды доехали?» перед
    login+nudge остаётся у вызывающего: раздача — его дело, здесь только
    побудка (#290).

    Ворота владения (#40) лечение проходит (force): diagnose выбирает его по
    состоянию, и каждое леченое состояние -- то, в котором сессия не работает
    (HUNG, аллокация pending/failed/lost, кончилась квота, свободный со
    старой спекой, #174). Чью аренду прошли -- в ответе.

    Действие без строки в TREAT -- отказ, а не рестарт (#370): рестарт стирает
    разговор и может убить работу, опаснее любого отказа."""
    action = issue["action"]
    cure = TREAT.get(action)
    if cure is None:
        return f"{action} failed: unknown action"
    try:
        return cure(issue["name"], issue["alloc"], {"owner": bus.login(), "force": True})
    except Exception as e:
        return f"{action} failed: {str(e)[:80]}"


def _stop(name, alloc, me):
    got = _cluster("stop", name=name, **me)
    return "alloc stop — Nomad will recreate it without backoff" + _note(got)


def _model(name, alloc, me):
    # Куда переводить папет, у которого кончилась квота текущей
    # модели (решение оператора 27.08: Fable → Opus). При вызове, а
    # не при импорте (#183): иначе всякий, кто
    # импортирует puppets (сервис кластера), числился бы читающим
    # настройку, которую применяет один doctor --fix.
    model = config.get("MOP_FALLBACK_MODEL")
    switch_model(Alloc.from_dict(alloc).node, name, model, force=True)
    return f"/model {model}"


def _login_nudge(name, alloc, me):
    # Свежие креды уже на узле (гейт вызывающего). Ход, оборванный
    # отказом API, сам не продолжается: будим сообщением тем же
    # глаголом send, что и `mop send`, -- адрес ответа мастера, ворота
    # владения агент проходит по force (#290). Не рестарт: он
    # поднимает claude начисто и стирает разговор.
    r = bus.request(Alloc.from_dict(alloc).node, "send", name=name, message=NUDGE,
                    priority="next", wait=0, owner=bus.login(), force=True,
                    timeout=bus.TIMEOUT)
    if "error" in r:
        raise RuntimeError(r["error"])
    return f"credentials pushed, nudged: {NUDGE}" + _note(r)


def _update(name, alloc, me):
    # Перерегистрация, а не рестарт: врапер живёт в спеке, и рестарт
    # аллокации поднял бы ту же старую. Клон переживает — меняется
    # только спека.
    # Ветка -- с остальной метой (#265): лечение не ставит папета
    # мастера обратно на origin/HEAD.
    meta = JobMeta.from_meta(_cluster("spec", name=name).get("meta"))
    if not meta.origin:
        raise RuntimeError(f"{name} has no origin in Meta")
    got = _cluster("update", name=name, origin=meta.origin,
                   branch=meta.branch, **me)
    return ("spec re-registered — the puppet comes up with the new wrapper"
            + _note(got))


def _restart(name, alloc, me):
    got = _cluster("restart", name=name, **me)
    return "restart" + _note(got)


# Действие -> лечение (#370): (name, alloc, me) -> строка итога. Рестарт --
# явная строка, а не ветка «всё прочее».
TREAT = {state.STOP: _stop, state.MODEL: _model, state.LOGIN_NUDGE: _login_nudge,
         state.UPDATE: _update, state.RESTART: _restart}


def _note(reply):
    """Хвост ответа лечения: чью аренду прошли ворота (#40)."""
    note = (reply or {}).get("owner_note")
    return f" ({note})" if note else ""


# ─── рецикл ───────────────────────────────────────────────────────────────
# Снос клона и target на узле: сотни тысяч inode. Одно число на всех, кто
# зовёт глагол wipe (#268): recycle отсюда и sweep пула.
WIPE_TIMEOUT = 600


def wipe(node, name, force=False, branch=None):
    """Глагол wipe напрямую, без остановки джоба. Агент сам откажет, если
    tmux-сессия жива: голый wipe — для уже остановленного папета, полный
    цикл (стоп → снос → подъём) — recycle. Чужой папет -- отказ агента с
    именем владельца (#40), force его проходит."""
    r = bus.request(node, "wipe", name=name, owner=bus.login(), force=force,
                    branch=branch, timeout=WIPE_TIMEOUT)
    if "error" in r:
        raise RuntimeError(r["error"])
    return r


def _wait_stopped(name):
    """Дождаться, пока последняя аллокация перестанет быть running.

    Останов джоба убивает задачу через KillTimeout (15с) и вместе с врапером —
    tmux-сессию. Сносить рабочую копию под живой сессией нельзя, поэтому ждём
    именно терминального статуса аллокации, а не «джоб dead» в API: между
    ними сидит остановка задачи на узле."""
    for _ in range(45):
        alloc = Alloc.from_dict(_cluster("alloc", name=name).get("alloc"))
        if not alloc or not alloc.running:
            return
        time.sleep(2)
    raise RuntimeError(f"allocation {name} won't stop — is the node alive?")


def classify_junk(answers, known):
    """Что на узлах лишнее. -> [{node, kind, name, detail, sweepable}].

    Чистая функция: узлы уже опрошены, Nomad уже спрошен. Разделять стоило
    не ради красоты — решение «что снести» должно быть проверяемо тестом, а
    не только живым пулом, где ошибка стоит чужой работы.

    Авторитет здесь Nomad, а не tmux. Узловой `mop driver sweep` судит по
    отсутствию сессии, и это верный признак ДЛЯ УЗЛА, который про Nomad не
    знает вовсе; но он же путает сироту с папетом между рестартами. С
    управляющей машины виден список джобов, и «тела нет в нём» — факт, а не
    догадка.

    Два рода находок, и они намеренно разного веса:

    СИРОТА — тело, которого нет среди джобов. Его папет удалён, а тело
    осталось: на гипервизоре это работающий контейнер с памятью и диском,
    которого больше никто не считает своим (поймано 22.09: pu-rugent-2 жил
    так, держа 118 ГБ тонкого тома). Сносится.

    СБОРОЧНОЕ ТЕЛО — имя шаблона в работающем состоянии. Запечатанный образ
    всегда стоит, так что работающий шаблон это либо сборка прямо сейчас,
    либо оборванная. Отсюда их НЕ РАЗЛИЧИТЬ, и поэтому такое тело только
    называется, но не сносится: снести чужую идущую сборку дороже, чем
    оставить мусор до следующего раза."""
    out = []
    for node in sorted(answers):
        a = answers[node] or {}
        work = a.get("work") or {}
        for name in sorted(a.get("bodies") or []):
            if name in known:
                continue
            # Строки work нет -- у тела нет клона (агент его не нашёл), и
            # спасать нечего: сносится, как и до #266. Есть -- решает одно
            # правило на всех, domain.holds_work.
            w = CloneFacts.from_dict(work.get(name))
            if w and holds_work(w):
                # Сирота с несохранённой работой остаётся. Джоба у неё нет,
                # значит вернуть её к делу уже нельзя, — но снесённое не
                # возвращается вовсе, а лежащий контейнер стоит только места.
                # Размен очевиден в одну сторону.
                out.append({
                    "node": node, "kind": "orphan", "name": name,
                    "detail": f"holds work: {w.dirty or 0} uncommitted, {w.ahead or 0} "
                              f"unpushed on {w.branch or '(detached)'}"
                              + (f", off home {w.home_branch}"
                                 if w.branch and w.home_branch
                                 and w.branch != w.home_branch else ""),
                    "sweepable": False})
                continue
            out.append({"node": node, "kind": "orphan", "name": name,
                        "detail": "no puppet with this name", "sweepable": True})
        for t in a.get("templates") or []:
            if not t.get("running"):
                continue
            out.append({"node": node, "kind": "build body", "name": t["name"],
                        "detail": f"vmid {t.get('vmid')} still running — "
                                  f"a build in flight, or one that broke",
                        "sweepable": False})
    return out


def delete(name, force=False):
    """Снести папета. -> {'node', 'body': 'destroyed'|'kept'|None, 'owner_note'}.

    У host тело — сам узел, и клон намеренно ОСТАЁТСЯ: он и есть ценность,
    прогретое дерево, которое переиспользует следующий подъём под тем же
    именем. Снести узел всё равно нельзя.

    У контейнерного драйвера «оставить клон» нечему: клон живёт ВНУТРИ тела,
    и оставленное тело — это работающий контейнер с зарезервированной
    памятью и занятым диском, которого больше никто не считает своим.
    Замерено 22.09 на hyper: после `mop delete` контейнер продолжал
    работать, а следующий `mop add` поднял ЕГО ЖЕ, со всем прежним
    содержимым, — то есть новый папет получил тело от старого вместе с его
    пакетами, кэшами и мусором, и «свежий папет на свежем образе» оказался
    неправдой, которую ничто не сообщало.

    Порядок тот же, что у рецикла, и по той же причине: остановить джоб →
    дождаться терминального статуса → сносить. Глагол wipe у контейнерного
    драйвера уносит тело целиком, и он же откажет, если tmux-сессия ещё
    жива, — снос под живой сессией недопустим независимо от того, что решил
    мастер."""
    alloc = Alloc.from_dict(_cluster("alloc", name=name).get("alloc"))
    node = alloc.node if alloc else None
    # Ворота владения (#40) -- у сервиса, до снятия джоба.
    note = _cluster("delete", name=name, owner=bus.login(),
                    force=force).get("owner_note")
    if not node:
        return {"node": None, "body": None, "owner_note": note}
    # Контракт драйвера — словарь, а НЕ модуль: `driver.require` отдаёт
    # проверенный реестром контракт, и флаг в нём лежит ключом is_container.
    # Модуль с атрибутом IS_CONTAINER возвращает только `driver.current()`, и
    # он про ЭТОТ узел, а нам нужен чужой — по имени. Драйвер приезжает вместе
    # с аллокацией: второго запроса за одним полем меты не делаем.
    drv = driver.of_node({"mop_driver": got.get("driver")}, node)
    if not driver.is_container(drv):
        return {"node": node, "body": "kept", "owner_note": note}
    _wait_stopped(name)
    wipe(node, name, force)
    return {"node": node, "body": "destroyed", "owner_note": note}


def recycle(name, workspace_of=None, force=False, branch=None):
    """Пересоздать папета на чистой рабочей копии. -> {node, owner_note}.

    Клон не переклонируется: сбрасывается на месте глаголом wipe (reset
    отслеживаемого + clean -xdff, который выметает и игнорируемое, но
    щадит подсеянное врапером: .env*, .providers), target-каталог
    удаляется целиком — он и есть почти весь объём.

    Цена рецикла зависит от драйвера, и с появлением тел она разошлась. У
    host первая сборка после рецикла долгая, поэтому там это крайняя мера, а
    не гигиена. У контейнерного драйвера тело сносится целиком и клонируется
    из прогретого образа проекта, где тулчейн и кэши уже лежат, — там рецикл
    стоит секунд.

    Порядок обязателен: остановить джоб → дождаться терминала → wipe →
    перерегистрировать спеку. Между решением «свободен» и сносом папету
    успевает прилететь задача (mop send идёт мимо мастера, у пула несколько
    мастеров), и остановленный джоб — единственное состояние, в котором
    сессии гарантированно нет. Перерегистрация, а не alloc_restart: врапер
    живёт в спеке джоба, рестарт аллокации поднял бы старую.

    workspace_of(origin) -> (текст workspace папета, происхождение) (#133,
    #334); None -- сервер оставляет положенный (рецикл без рабочей копии,
    `mop server gc`).

    branch -- ветка мастера (#257), её знает командлет рабочей копии; None --
    та, с которой папет заведён (мета джоба): рецикл без рабочей копии (gc)
    её не теряет. Окружение процесса здесь не читается (#367): gc
    операторский и рециклит папетов всех проектов, и git config mop.branch
    или MOP_BRANCH оператора перекрыли бы ветку чужого папета.

    Ворота владения (#40) -- на первом шаге, останове: чужой папет
    отказывает до того, как что-то остановлено; force идёт во все три
    шага."""
    meta = JobMeta.from_meta(_cluster("spec", name=name).get("meta"))
    origin = meta.origin
    if not origin:
        raise RuntimeError(f"{name} has no origin in Meta — is this even a puppet?")
    alloc = Alloc.from_dict(_cluster("alloc", name=name).get("alloc"))
    node = alloc.node if alloc else None
    if not node:
        raise RuntimeError(f"{name} has no allocation — nothing to recycle")

    branch = branch or meta.branch
    me = {"owner": bus.login(), "force": force}
    note = _cluster("delete", name=name, purge=False, **me).get("owner_note")
    _wait_stopped(name)
    try:
        wipe(node, name, force, branch=branch)
    except RuntimeError as e:
        raise RuntimeError(f"{e}; job is stopped — after fixing the node "
                           f"retry: mop recycle {name}")
    # workspace_of -> (текст, происхождение) (#334): происхождение едет
    # рядом и возвращается -- командлет называет, что уехало.
    sent = None
    fields = {}
    if workspace_of:
        text, sent = workspace_of(origin)
        fields = {"workspace": text, "bootstrap_sent": sent}
    got = _cluster("update", name=name, origin=origin, branch=branch,
                   **me, **fields)
    # Метка регистрации (#334): командлет ждёт итог прогона по ней.
    return {"node": node, "owner_note": note, "bootstrap_sent": sent,
            **{k: got[k] for k in ("bootstrap_marker", "replaced") if k in got}}


# ─── ввод в TUI папета ───────────────────────────────────────────────────
# Всё здесь адресуется узлом, а не аллокацией: alloc exec умер вместе со своей
# адресацией, и агент подписан на субъект узла.
def type_command(node, name, command, force=False):
    """Напечатать слэш-команду в tmux-пейн папета и вернуть экран после неё.

    Печатью, а не сообщением по каналу: слэш-команды через канал не проходят
    (сообщение кладётся в очередь с skipSlashCommands), а у папета с
    исчерпанной квотой любой ход падает, не начавшись — слэш-команду же
    исполняет сам TUI, ход на неё не тратится.

    Белый список команд проверяет агент: проверка на этой стороне осталась бы
    подсказкой пользователю, а не правом. Владельца -- тоже (#40): чужой
    папет -- отказ агента с именем, force его проходит."""
    r = bus.request(node, "type", name=name, command=command, owner=bus.login(),
                    force=force, timeout=45)
    if "error" in r:
        raise RuntimeError(r["error"])
    return r.get("screen") or ""


def press_enter(node, name, force=False):
    """Подтвердить диалог. Только увидев его: слепой Enter на папет без
    диалога отправил бы пустой ход."""
    return type_command(node, name, "", force)


def switch_model(node, name, model, force=False):
    """Перевести папет на другую модель, напечатав /model в его tmux-пейн.

    `/model` не переключает молча — он спрашивает «Switch model?» с уже
    выделенным «Yes». Подтверждаем вторым Enter, но только увидев диалог."""
    out = type_command(node, name, f"/model {model}", force)
    if "switch model?" in out.lower():
        out = press_enter(node, name, force)
    if "switch model?" in out.lower():
        raise RuntimeError("model switch dialog did not close")


def alloc_stderr(name, lines):
    """Хвост stderr задачи аллокации папета глаголом `stderr` (#333):
    [строки], пусто -- аллокации нет или stderr пуст."""
    return _cluster("stderr", name=name, lines=lines).get("lines") or []


def pane_lines(node, name):
    """Весь буфер tmux-пейна папета (история + экран)."""
    r = bus.request(node, "tail", name=name)
    if "error" in r:
        raise RuntimeError(f"tmux in {name}: {r['error']}")
    return r.get("lines") or []
