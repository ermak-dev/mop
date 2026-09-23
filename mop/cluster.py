"""Сервис кластера: Nomad за шиной.

Единственное на установке, что говорит с Nomad от чужого имени. Мастер и
оператор просят глаголом в субъект `mop.<проект>.cluster.rpc`, сервис решает,
можно ли, и только потом идёт в API. Nomad становится деталью сервера: токен
живёт здесь, и больше нигде (#80).

Зачем. Management-токен Nomad не знает про проекты вовсе: имея его, мастер
проекта снимал джоб любого другого — шину проекты делят кредами, а Nomad не
делит ничем. Проверка та же, что у агента узла: проект берётся из СУБЪЕКТА,
субъект проверен правами NATS, и он сверяется с origin джоба (`Meta.origin`),
то есть с единственной правдой о том, чей это папет.

Почему свой субъект, а не `server.rpc` bootstrap'а. В субъект сервера имеет
право писать агент узла (`node-<узел>` в nats-server.conf: узел просит
bootstrap песочницы за папета). Положи туда глаголы над Nomad — и любой узел
регистрирует и снимает джобы. Свой субъект прав никому не добавляет: у
`master-<проект>` уже есть весь `mop.<проект>.>`, у папета и узла — нет.

Спеку джоба собирает сервер, а не проситель. Глагол — `add(origin, profile)`,
не `register(spec)`: приняв готовую спеку, сервис отдал бы исполнение кода на
узлах любому, кто дотянулся до шины, и токен на сервере не защищал бы ничего.

Подписчик — mop-cluster, юнит на сервере; проверяется `mop cluster check`.
Чистая часть (права) — tests/cluster.py.
"""
import asyncio
import json

from . import bus, nodes, nomad, projects, puppets

# Токен субъекта. Не "server": туда пишет узел, см. докстринг модуля.
CHANNEL = "cluster"

# Глаголы проекта: про его собственных папетов.
PROJECT_VERBS = ("ping", "roster", "pool", "add", "update", "restart", "stop",
                 "delete", "alloc", "spec")
# Глаголы оператора: про машины. Место на узле общее для всех его жильцов, а
# увод папетов с машины касается всех проектов разом — мастеру не показываем.
ADMIN_VERBS = ("nodes", "drain", "up", "forget", "meta")
VERBS = PROJECT_VERBS + ADMIN_VERBS
# Глаголы, которые называют джоб: у них проверяется владелец.
NAMED_VERBS = ("update", "restart", "stop", "delete", "alloc", "spec")
# Из них те, что ДЕЛАЮТ: им отсутствие джоба — отказ. Читающему `alloc` нет:
# `puppets.delete` спрашивает аллокацию УЖЕ СНЯТОГО джоба, дожидаясь, пока
# тот перестанет быть running, и отказ там оставлял тело работать сиротой
# (#89). `spec` в список входит: на его отказе стоит `lib.require_job`.
ACTING_VERBS = ("update", "restart", "stop", "delete", "spec")


# ─── чистое: кому что можно ──────────────────────────────────────────────
def project_of(subject):
    """Проект из субъекта `mop.<проект>.cluster.rpc`.

    Из субъекта, а не из тела запроса: субъект проверен правами NATS, тело
    пишет кто угодно."""
    parts = (subject or "").split(".")
    return parts[1] if len(parts) > 2 else ""


def refusal(project, verb, origin=None, name=None, job_exists=False,
            new_origin=None):
    """Почему запрос отклонён. -> строка или None.

    origin — `Meta.origin` джоба для именующих глаголов, либо origin проекта
    для `add`; он же и есть свидетельство, что джоб существует. job_exists
    нужен ровно для третьего случая: джоб ЕСТЬ, а метки нет. Тогда это отказ,
    а не пропуск — чей непомеченный джоб, из него самого не узнать, и отдать
    его мастеру значит отдать наугад (puppets.visible показывает такие только
    оператору по той же причине). Джоба нет вовсе — не отказ прав: об этом
    скажет сам глагол, иначе опечатка в имени читается как «нет прав»."""
    if verb not in VERBS:
        return (f"no such verb {verb}; project verbs: {', '.join(PROJECT_VERBS)}; "
                f"operator verbs: {', '.join(ADMIN_VERBS)}")
    operator = project == bus.ADMIN
    if verb in ADMIN_VERBS and not operator:
        return (f"{verb} is the operator's verb: a node is shared by every project "
                f"on it, and {project} sees only its own puppets")
    if operator:
        return None
    if verb == "add":
        return _foreign_origin(project, origin)
    if verb == "update" and new_origin:
        # Смена репозитория — в границах проекта: иначе папет проекта клонирует
        # чужой репозиторий ключом пула, оставаясь на кредах своего проекта.
        why = _foreign_origin(project, new_origin)
        if why:
            return why
    if verb in NAMED_VERBS and (origin or job_exists):
        if not origin:
            return (f"{name} carries no origin: an old registration, and whose it "
                    f"is cannot be told from the job — only the operator reaches it")
        owner = puppets.project_of(origin)
        if owner != project:
            return f"{name} belongs to project {owner}, not {project}"
    return None


def over_limit(project, count, limit):
    """Отказ `add` по потолку папетов проекта (#107). -> строка или None.

    Отказ называет проект, счёт и потолок: иначе «папет не заводится» ищут в
    Nomad и в слотах узлов."""
    if limit is None or count < limit:
        return None
    return (f"project {project} has {count} puppet(s) and its limit is {limit}: "
            f"delete one, or raise the limit on the controller: "
            f"mop project limit {project} <N|none>")


def _foreign_origin(project, origin):
    """Чужой ли это origin для проекта. -> строка или None."""
    owner = puppets.project_of(origin or "")
    if owner != project:
        return (f"{origin} belongs to project {owner}, not {project}: "
                f"a master works inside its own project only")
    return None


# ─── глаголы: Nomad ──────────────────────────────────────────────────────
def _owner(name):
    """(origin джоба, есть ли джоб). Разные исходы у «нет джоба» и «джоб без
    метки» — на них по-разному отвечает refusal()."""
    try:
        job = nomad.get_job(name)
    except Exception:
        return None, False
    if not job:
        return None, False
    return ((job.get("Meta") or {}).get("origin") or None), True


def _ping(project, req):
    """Жив ли сервис и видит ли он Nomad. Отвечает всем: мастеру надо уметь
    отличить «сервис лёг» от «Nomad не отвечает», не имея ни того, ни другого
    под рукой."""
    try:
        seen = len(nomad.client().nodes.get_nodes())
        cluster_ok, why = True, None
    except Exception as e:
        seen, cluster_ok, why = 0, False, nomad.describe_error(e)
    return {"ok": True, "nomad": nomad.ADDR, "reachable": cluster_ok,
            "nodes": seen, "error": why, "project": project,
            "verbs": list(VERBS)}


def _live_count(target):
    """Сколько джобов проекта живо. По префиксу имени: имена `pu-<проект>-<n>`
    строятся из того же basename, что и проект (next_name)."""
    got = nomad.client().jobs.get_jobs(prefix=f"{puppets.JOB_PREFIX}{target}-")
    return sum(1 for j in got if j.get("Status") != "dead")


def _add(project, req):
    origin = req.get("origin")
    # Лимит -- проекта из origin, а не просителя: оператор через admin
    # заводит папета тому же проекту и упирается в тот же потолок. Файл
    # читается на каждый запрос: правка лимита доезжает без рестарта.
    target = puppets.project_of(origin)
    why = over_limit(target, _live_count(target),
                     projects.read_limits().get(target))
    if why:
        return {"error": why}
    name = puppets.next_name(target)
    nomad.register(puppets.job_spec(name, origin, req.get("profile")))
    return {"ok": True, "name": name, "origin": origin}


def _update(project, req):
    # Спеку собирает сервер: `register(spec)` глаголом не бывает, иначе
    # проситель кладёт на узел что хочет (докстринг модуля).
    name = req["name"]
    nomad.register(puppets.job_spec(name, req.get("origin"), req.get("profile"),
                                    cont=bool(req.get("cont"))))
    return {"ok": True, "name": name}


def _restart(project, req):
    alloc = nomad.latest_alloc(req["name"])
    if not alloc:
        return {"error": f"{req['name']} has no allocation to restart"}
    nomad.alloc_restart(alloc["ID"])
    return {"ok": True, "alloc": alloc["ID"], "node": alloc.get("NodeName")}


def _stop(project, req):
    alloc = nomad.latest_alloc(req["name"])
    if not alloc:
        return {"error": f"{req['name']} has no allocation to stop"}
    nomad.alloc_stop(alloc["ID"])
    return {"ok": True, "alloc": alloc["ID"]}


def _delete(project, req):
    """Снять джоб. purge=False оставляет его в истории остановленным — так
    работает рецикл: джоб останавливается, рабочая копия сносится, и та же
    спека поднимается обратно."""
    nomad.deregister(req["name"], purge=bool(req.get("purge", True)))
    return {"ok": True, "name": req["name"]}


def _alloc(project, req):
    """Аллокация папета плюс драйвер её узла.

    Драйвер здесь, а не отдельным глаголом: `mop attach` спрашивает ровно эти
    две вещи вместе — где папет стоит и чем в него входят, — и второй запрос
    по сети ради одного поля меты был бы платой ни за что."""
    try:
        alloc = nomad.latest_alloc(req["name"])
    except Exception:
        # Джоба уже нет — это ответ, а не отказ: так `puppets.delete` ждёт,
        # пока снятый джоб перестанет быть running.
        alloc = None
    if not alloc:
        return {"ok": True, "alloc": None, "driver": None}
    slim = {k: alloc.get(k) for k in puppets.ALLOC_FIELDS}
    meta = nomad.node_meta(alloc["NodeName"]) or {}
    return {"ok": True, "alloc": slim, "driver": meta.get("mop_driver")}


def _spec(project, req):
    job = nomad.get_job(req["name"])
    if not job:
        return {"error": f"no job {req['name']}"}
    return {"ok": True, "meta": job.get("Meta") or {},
            "status": job.get("Status"), "stale": puppets.spec_is_stale(job)}


def _roster(project, req):
    """Ростер глазами проекта: джоб плюс его аллокация. Отбор — тот же
    visible(): непомеченные видит только оператор, и это не два правила, а
    одно. Аллокация едет вместе с джобом — клиенту иначе пришлось бы идти за
    каждой отдельным запросом по сети.

    stale=True добавляет вердикт об устаревшей спеке, и только по просьбе: он
    стоит вызова API на каждый джоб, а нужен одному `mop doctor`. Платить за
    него в каждом `mop list` было бы платой за чужой глагол."""
    return {"ok": True, "items": puppets.nomad_items(project, stale=bool(req.get("stale")))}


def _pool(project, req):
    """Ёмкость узлов пула. Проекту это положено: по свободным слотам мастер
    решает, заводить ли папета. Кто ещё живёт на узле — глагол `nodes`."""
    return {"ok": True, "nodes": puppets.nomad_pool()}


def _nodes(project, req):
    return {"ok": True, "nodes": nodes.nomad_rows()}


def _drain(project, req):
    nomad.node_drain(req["node"], int(req.get("deadline") or 300))
    return {"ok": True, "node": req["node"]}


def _up(project, req):
    nomad.node_eligibility(req["node"], True)
    return {"ok": True, "node": req["node"]}


def _forget(project, req):
    node = req["node"]
    why = nomad.forget_refusal(node, nomad.node_allocs(node))
    if why:
        return {"error": why}
    nomad.node_forget(node)
    return {"ok": True, "node": node}


def _meta(project, req):
    nomad.set_node_meta(req["node"], req.get("updates") or {})
    return {"ok": True, "node": req["node"], "meta": nomad.node_meta(req["node"])}


HANDLERS = {"ping": _ping, "roster": _roster, "pool": _pool, "add": _add, "update": _update, "restart": _restart,
            "stop": _stop, "delete": _delete, "alloc": _alloc, "spec": _spec,
            "nodes": _nodes, "drain": _drain, "up": _up, "forget": _forget,
            "meta": _meta}


def answer(project, req):
    """Запрос от имени проекта -> ответ (dict). Синхронно: зовут в потоке.

    Владельца джоба узнаём ДО проверки и проверяем по нему, а не по имени:
    имя `pu-<проект>-<n>` собирается из того же basename, но правду о том,
    чей это папет, несёт только Meta.origin."""
    verb = req.get("verb")
    name = req.get("name")
    origin, exists = (None, False)
    if verb in NAMED_VERBS and name:
        origin, exists = _owner(name)
    why = refusal(project, verb, origin=origin or req.get("origin"), name=name,
                  job_exists=exists, new_origin=req.get("new_origin"))
    if why:
        return {"error": why}
    if verb in ACTING_VERBS and name and not exists:
        return {"error": f"no job {name} in the cluster"}
    try:
        return HANDLERS[verb](project, req)
    except KeyError as e:
        return {"error": f"{verb}: missing field {e}"}
    except Exception as e:
        return {"error": f"{verb}: {nomad.describe_error(e)}"}


# ─── подписчик ───────────────────────────────────────────────────────────
async def _handle(msg):
    project = project_of(msg.subject)
    try:
        req = json.loads(msg.data.decode())
    except ValueError:
        req = {}
    # В отдельном потоке: вызовы Nomad блокирующие, а петля обязана отвечать
    # остальным, пока один запрос ждёт HTTP.
    out = await asyncio.get_running_loop().run_in_executor(None, answer, project, req)
    print(f"{project}.{req.get('verb')} {req.get('name') or req.get('node') or ''}: "
          f"{out.get('error') or 'ok'}", flush=True)
    try:
        await msg.respond(json.dumps(out, ensure_ascii=False).encode())
    except Exception:
        pass


async def serve():
    """Подписчик сервера. Креды — оператора (admin): сервис слушает все
    проекты, а разделяет их проверкой проекта из субъекта."""
    import nats
    c = bus.config()
    nc = await nats.connect(**bus.auth(c), name="mop-cluster",
                            allow_reconnect=True, max_reconnect_attempts=-1,
                            reconnect_time_wait=2)

    async def on_rpc(msg):
        asyncio.create_task(_handle(msg))

    subj = bus.cluster_subject("*")
    await nc.subscribe(subj, cb=on_rpc)
    print(f"mop-cluster: subscribed to {subj}, Nomad at {nomad.ADDR}", flush=True)
    await asyncio.Event().wait()
