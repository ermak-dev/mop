"""Папеты пула глазами мастера: ростер через шину, диагноз и лечение, рецикл,
ввод в TUI папета. Спека джоба — mop/spec.py, Nomad-половина ростера — сервис
кластера (mop/cluster.py), вердикт о состоянии — mop/state.py (#145, #152).

Модуль возвращает данные и ничего не печатает. Форматирование живёт во
фронтендах (командлеты в bin/ печатают таблицы, mop mcp отдаёт то же самое
модели) — иначе второй фронтенд неизбежно начал бы разбирать чужой текст.
"""
import os
import time

from . import bus, config, driver, lease, llm, spec
from .domain import Owner
from .state import PuppetRow, action_for, failing_row, silent, spec_action, verdict

PROJECT = config.PROJECT

# Значения этой установки — .env поверх дефолтов; см. mop/config.py.
HOME = spec.HOME                           # $HOME на узлах пула
# Соглашение об имени папета (префикс, разбор, каталоги) живёт в реестре
# драйверов: это единственный stdlib-модуль, который читают и мастер, и узел.
# Здесь — только имена, под которыми его знает мастер.
JOB_PREFIX = driver.PREFIX
project_of_name = driver.project_of_name
clone_dir = driver.clone_dir

# ─── LLM-профили ─────────────────────────────────────────────────────────
# Папет — всегда claude code; профиль меняет ровно одно: куда он ходит за
# токенами. Anthropic-совместимый эндпоинт провайдера (ANTHROPIC_BASE_URL),
# ключ (ANTHROPIC_AUTH_TOKEN) и карта имён моделей opus/sonnet/haiku в модели
# провайдера — больше в папет ничего не меняется, поэтому tmux, tail, doctor
# и детект залипаний работают одинаково для любого профиля.
#
# Сами профили — плагины в mop/llm/ (один файл = один профиль, имя файла =
# имя). Здесь только потребление. Дефолт — настройка установки: контора на
# одном провайдере меняет дефолт, а не каждую команду.
# Источник правды по секретам проекта — .env рядом с кодом. Всё, что задаёт
# человек, лежит там; порождаемое само (пароли NATS, токен Nomad, логин
# claude.ai) — не там и туда не попадает.
LOCAL_KEYS_FILE = config.ENV_FILE   # тот же файл, одно определение (#217)
# Подмножество .env, которое уезжает на узлы: только ключи, названные
# профилями. Секреты MCP-серверов установки едут иначе -- их прописывает
# sandbox.yaml прямо в регистрацию сервера.
SECRETS_FILE = driver.SECRETS_FILE                 # копия на узле пула


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
        origin = (j.get("Meta") or {}).get("origin")
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


def facts(node, name):
    """Факты об одном папете с его узла."""
    return bus.request(node, "state", name=name)


# ─── сводки для фронтендов ───────────────────────────────────────────────
def failing(alloc):
    """Строка состояния падающего папета из ответа `alloc`, либо None (#126)."""
    a = alloc or {}
    row = failing_row(a.get("ClientStatus"), a.get("task"), a.get("reason"))
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
    if not a or a.get("ClientStatus") != "running":
        raise LookupError(not_running(name, a))
    return a, got.get("driver")


def running_alloc(name):
    """Работающая аллокация папета (см. running)."""
    return running(name)[0]


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
        alloc = item["alloc"]
        if alloc and alloc["ClientStatus"] == "running":
            by_node.setdefault(alloc["NodeName"], []).append(item)
        got.append(item)

    # Шина легла целиком — ростер всё равно показываем. Он приходит из Nomad и
    # к шине отношения не имеет; уронить `list` вместе с ней значит оставить
    # мастера без единственной картины пула ровно тогда, когда что-то сломалось.
    try:
        answers = bus.request_many({
            node: {"verb": "states", "names": [i["job"]["ID"] for i in its]}
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
    clone = ((facts or {}).get("clone")) or None
    owner = Owner.from_dict((clone or {}).get("owner"))
    return owner.user if lease.live(owner, clone, now) else None


def rows_from(items):
    """Строки без обмера из ростера."""
    return [_row(i) for i in items]


def _row(item, disk_kb=None):
    job, alloc = item["job"], item["alloc"]
    meta = job.get("Meta") or {}
    status = item["error"] or (alloc["ClientStatus"] if alloc else job.get("Status", "?"))
    state, kind = item["state"] or "-", item.get("kind")
    # Падающий на старте -- failing с причиной, а не pending (#126).
    failing = failing_row(status, item.get("task"), item.get("reason"))
    if failing and not item["error"]:
        status, state = failing
        kind = "failing"
    return PuppetRow(
        name=job["ID"],
        node=alloc["NodeName"] if alloc else "-",
        alloc_status=status,
        state=state,
        kind=kind,
        owner=item.get("owner") or "-",
        llm=llm.of_meta(meta),
        origin=meta.get("origin", "?"),
        disk_kb=disk_kb,
    )


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
        alloc = i["alloc"]
        if alloc and alloc["ClientStatus"] == "running":
            asked[i["job"]["ID"]] = (alloc["NodeName"],
                                     {"verb": "sizes", "names": [i["job"]["ID"]]})
        else:
            rest.append(i)
    by_name = {i["job"]["ID"]: i for i in items}

    # Папета, у которых спрашивать некого, ждать нечего — они уходят первыми.
    for i in rest:
        yield _row(i)
    try:
        for name, answer in bus.request_stream(asked, timeout=45):
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
    (mop/state.py), state — его строка. disk_kb — клон плюс target,
    обмеряется спросом; None — du не доехал, это прочерк, а не ноль.

    sizes=False — без обмера вовсе: дашборд (mop/web.py) опрашивает
    состояния в разы чаще, чем место, и ждать du на каждом круге значило бы
    показывать состояние с опозданием на обмер. Место он берёт отдельно,
    puppet_sizes, своим расписанием."""
    if not sizes:
        return sorted(rows_from(roster()), key=lambda r: r.name)
    return sorted(puppet_rows_stream(), key=lambda r: r.name)


def puppet_sizes(rows, timeout=45):
    """Обмер места по строкам puppet_rows: {имя: КБ}. Спрашиваются только
    те, у кого бежит аллокация; кого не обмерили — в ответе нет, и это
    прочерк у вызывающего. Легла шина — пустой ответ, не исключение: место
    здесь не главное, а состояние уже показано."""
    asked = {}
    for r in rows:
        if r.alloc_status == "running" and r.node != "-":
            asked[r.name] = (r.node, {"verb": "sizes", "names": [r.name]})
    out = {}
    try:
        for name, answer in bus.request_stream(asked, timeout=timeout):
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
    return bus.call_cluster("pool").get("nodes") or []


def ready_nodes():
    """Имена узлов, на которые Nomad вообще станет что-то ставить. Через шину:
    тот же ответ, что раньше давал nomad.ready_nodes() на машине оператора."""
    return {n["name"] for n in pool()
            if n.get("status") == "ready" and n.get("eligible", True)}


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
        failing = failing_row(alloc and alloc["ClientStatus"], item.get("task"),
                              item.get("reason"))
        if failing:
            issues.append({"name": job["ID"], "alloc": alloc,
                           "diagnosis": failing[1], "action": None})
            continue
        if not alloc or alloc["ClientStatus"] in ("lost", "unknown", "failed",
                                                  "pending"):
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
    if alloc and alloc["ClientStatus"] in ("pending", "failed"):
        return {"name": name, "alloc": alloc, "action": "stop",
                "diagnosis": f"allocation {alloc['ClientStatus']} (restart-backoff?)"}
    if alloc:
        return {"name": name, "alloc": alloc, "action": "stop",
                "diagnosis": f"allocation {alloc['ClientStatus']}"}
    if spec.queued(job) and unserved == "memory":
        # Просьба проекта больше, чем готова дать любая машина с его образом:
        # ни ожидание, ни сборка образа не помогут.
        project = project_of((job.get("Meta") or {}).get("origin") or "")
        return {"name": name, "alloc": None, "action": None,
                "diagnosis": f"queued — no ready node of {project} takes a "
                             f"{ceiling} MB puppet (node meta mop_mem_cap_mb): "
                             f"lower MOP_MEM_MB in {project}'s .mop or raise "
                             f"mop_body_mem_cap_mb of a node"}
    if spec.queued(job) and unserved:
        # Слоты тут ни при чём: ограничение размещения по образу (#10) не
        # пускает никуда, и ожидание не вылечит ничего.
        origin = (job.get("Meta") or {}).get("origin") or "<origin>"
        return {"name": name, "alloc": None, "action": None,
                "diagnosis": f"queued — no ready node has an image of "
                             f"{project_of(origin)}: mop project add {origin}"}
    if spec.queued(job):
        return {"name": name, "alloc": None, "action": None,
                "diagnosis": "queued — no free slots in the pool"}
    return {"name": name, "alloc": None, "action": None, "diagnosis": "no allocation"}


def treat(issue):
    """Применить лечение к одной проблеме из diagnose. -> что вышло, строкой.

    Одно место на оба фронтенда, `mop doctor --fix` и инструмент doctor в MCP:
    пока лечение жило в каждом своём, они разошлись — CLI перерегистрировал
    спеку по диагнозу `update`, а MCP на тот же диагноз делал рестарт, то есть
    поднимал ту же старую спеку (#47). Гейт «креды доехали?» перед
    login+restart остаётся у вызывающего: раздача — его дело.

    Ворота владения (#40) лечение проходит (force): diagnose выбирает его по
    состоянию, и каждое леченое состояние -- то, в котором сессия не работает
    (HUNG, аллокация pending/failed/lost, кончилась квота, свободный со
    старой спекой, #174). Чью аренду прошли -- в ответе."""
    action, alloc, name = issue["action"], issue["alloc"], issue["name"]
    me = {"owner": bus.login(), "force": True}
    try:
        if action == "stop":
            got = _cluster("stop", name=name, **me)
            return "alloc stop — Nomad will recreate it without backoff" + _note(got)
        if action == "model":
            # Куда переводить папет, у которого кончилась квота текущей
            # модели (решение оператора 27.08: Fable → Opus). При вызове, а
            # не при импорте (#183): иначе всякий, кто
            # импортирует puppets (сервис кластера), числился бы читающим
            # настройку, которую применяет один doctor --fix.
            model = config.get("MOP_FALLBACK_MODEL")
            switch_model(alloc["NodeName"], name, model, force=True)
            return f"/model {model}"
        if action == "update":
            # Перерегистрация, а не рестарт: врапер живёт в спеке, и рестарт
            # аллокации поднял бы ту же старую. Клон переживает — меняется
            # только спека.
            meta = _cluster("spec", name=name).get("meta") or {}
            got = _cluster("update", name=name, origin=meta["origin"],
                           profile=meta.get("llm"), **me)
            return ("spec re-registered — the puppet comes up with the new wrapper"
                    + _note(got))
        got = _cluster("restart", name=name, **me)
        return "restart" + _note(got)
    except Exception as e:
        return f"{action} failed: {str(e)[:80]}"


def _note(reply):
    """Хвост ответа лечения: чью аренду прошли ворота (#40)."""
    note = (reply or {}).get("owner_note")
    return f" ({note})" if note else ""


# ─── рецикл ───────────────────────────────────────────────────────────────
def wipe(node, name, force=False):
    """Глагол wipe напрямую, без остановки джоба. Агент сам откажет, если
    tmux-сессия жива: голый wipe — для уже остановленного папета, полный
    цикл (стоп → снос → подъём) — recycle. Чужой папет -- отказ агента с
    именем владельца (#40), force его проходит."""
    r = bus.request(node, "wipe", name=name, owner=bus.login(), force=force,
                    timeout=600)
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
        alloc = _cluster("alloc", name=name).get("alloc")
        if not alloc or alloc["ClientStatus"] != "running":
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
            w = work.get(name) or {}
            dirty, ahead = w.get("dirty") or 0, w.get("ahead") or 0
            if dirty or ahead:
                # Сирота с несохранённой работой остаётся. Джоба у неё нет,
                # значит вернуть её к делу уже нельзя, — но снесённое не
                # возвращается вовсе, а лежащий контейнер стоит только места.
                # Размен очевиден в одну сторону.
                out.append({
                    "node": node, "kind": "orphan", "name": name,
                    "detail": f"holds work: {dirty} uncommitted, {ahead} "
                              f"unpushed on {w.get('cur') or '(detached)'}",
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
    got = _cluster("alloc", name=name)
    alloc = got.get("alloc")
    node = alloc["NodeName"] if alloc else None
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


def recycle(name, workspace_of=None, force=False):
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

    workspace_of(origin) -> текст workspace папета (#133); None -- сервер
    оставляет положенный (рецикл без рабочей копии, `mop gc`).

    Ворота владения (#40) -- на первом шаге, останове: чужой папет
    отказывает до того, как что-то остановлено; force идёт во все три
    шага."""
    meta = _cluster("spec", name=name).get("meta") or {}
    origin = meta.get("origin")
    if not origin:
        raise RuntimeError(f"{name} has no origin in Meta — is this even a puppet?")
    profile = llm.of_meta(meta)
    alloc = _cluster("alloc", name=name).get("alloc")
    node = alloc["NodeName"] if alloc else None
    if not node:
        raise RuntimeError(f"{name} has no allocation — nothing to recycle")

    me = {"owner": bus.login(), "force": force}
    note = _cluster("delete", name=name, purge=False, **me).get("owner_note")
    _wait_stopped(name)
    try:
        wipe(node, name, force)
    except RuntimeError as e:
        raise RuntimeError(f"{e}; job is stopped — after fixing the node "
                           f"retry: mop recycle {name}")
    fields = {"workspace": workspace_of(origin)} if workspace_of else {}
    _cluster("update", name=name, origin=origin, profile=profile, **me, **fields)
    return {"node": node, "owner_note": note}


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


def pane_lines(node, name):
    """Весь буфер tmux-пейна папета (история + экран)."""
    r = bus.request(node, "tail", name=name)
    if "error" in r:
        raise RuntimeError(f"tmux in {name}: {r['error']}")
    return r.get("lines") or []
