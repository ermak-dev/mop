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
import json
import os
import threading
import time

import base64

from . import (bootstrap, bus, busnames, config, creds, landing, lease, natsconf,
               nodes, nomad, project_secrets, projects, puppets, service, spec,
               state)
from .domain import Owner, Project, Verb

# Токен субъекта. Не "server": туда пишет узел, см. докстринг модуля.
CHANNEL = "cluster"

# Кому глагол дан -- одна таблица VERBS в конце модуля, рядом с обработчиками
# (#173); прежние наборы выводятся из неё.
PROJECT, SECRET, ADMIN = "project", "secret", "admin"


def _row(verb):
    """Строка таблицы по глаголу либо None. Нестроковый глагол -- неизвестный:
    список ключом таблицы бросил бы TypeError (#168 у агента)."""
    return VERBS.get(verb) if isinstance(verb, str) else None


# ─── чистое: кому что можно ──────────────────────────────────────────────
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
    spec = _row(verb)
    if spec is None:
        return (f"no such verb {verb}; project verbs: {', '.join(PROJECT_VERBS)}; "
                f"operator verbs: {', '.join(ADMIN_VERBS)}")
    operator = project == bus.ADMIN
    if spec.scope == SECRET and operator:
        return (f"{verb}: secrets belong to a project — ask on "
                f"mop.<project>.cluster.rpc, not the operator's subject")
    if spec.scope == ADMIN and not operator:
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
    if spec.named and (origin or job_exists):
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
            f"delete one, or raise the limit: mop project limit {project} <N|none>")


def delete_refusal(name, dropped, alive):
    """Отказ снять проект. -> строка или None. Чистая функция.

    dropped -- строки реестра, которые снятие убрало бы; пусто значит «такого
    проекта нет», и это отказ с именем: опечатка иначе читалась бы снятием.
    alive -- джобы проекта: мастер потерял бы шину под живыми папетами, а
    они работали бы дальше, и услышать их было бы некому."""
    if not dropped:
        return f"no project {name} in the registry; what the pool serves: mop project list"
    if alive:
        return (f"{name} still has puppets: {', '.join(sorted(alive))}. "
                f"Delete them first: mop delete {sorted(alive)[0]}")
    return None


def _foreign_origin(project, origin):
    """Чужой ли это origin для проекта. -> строка или None."""
    owner = puppets.project_of(origin or "")
    if owner != project:
        return (f"{origin} belongs to project {owner}, not {project}: "
                f"a master works inside its own project only")
    return None


# ─── Nomad-половина ростера (#152) ───────────────────────────────────────
# Жила в puppets.py рядом с клиентом шины мастера, хотя звал её только этот
# сервис: он один говорит с Nomad от чужого имени (docs/CLUSTER.md).
# Поля аллокации, которые читает клиент. Возим их, а не аллокацию целиком:
# в ней TaskStates и события, и тринадцать таких ответов упираются в предел
# сообщения шины на пустом месте.
ALLOC_FIELDS = ("ID", "JobID", "NodeName", "ClientStatus", "DesiredStatus")


def nomad_jobs(project=None):
    """Джобы папетов из Nomad. Зовёт это только сервис кластера: он один
    говорит с Nomad от чужого имени (docs/CLUSTER.md)."""
    listing = nomad.client().jobs.get_jobs(prefix=puppets.JOB_PREFIX, meta=True)
    return puppets.visible(listing, project)


def task_and_reason(alloc):
    """Сводка задачи и, у падающей, причина из stderr (#126). Только сервис
    кластера: у него Nomad. stderr читается лишь у падающих -- это запрос к
    клиенту Nomad на узле, и платить его за здоровых незачем."""
    task = state.task_summary(alloc)
    reason = None
    if state.failing_row(alloc.get("ClientStatus"), task, None):
        try:
            reason = state.failure_reason(
                nomad.alloc_stderr(alloc["ID"], state.task_name(alloc)))
        except Exception:
            pass
    return task, reason


def nomad_items(project=None, stale=False):
    """Ростер из Nomad: джоб плюс его аллокация. Тоже только сервис кластера.

    Аллокация приезжает вместе с джобом намеренно: клиент за ней отдельно уже
    не сходит, а N+1 запрос по шине вместо N+1 вызова API — та же цена, но с
    сетью между ними."""
    items, placement = [], None
    for j in sorted(nomad_jobs(project), key=lambda j: j["ID"]):
        alloc, err, task, reason = None, None, None, None
        try:
            got = nomad.latest_alloc(j["ID"])
            alloc = {k: got.get(k) for k in ALLOC_FIELDS} if got else None
            if got:
                task, reason = task_and_reason(got)
        except Exception as e:
            err = nomad.describe_error(e)
        item = {"job": j, "alloc": alloc, "error": err, "task": task, "reason": reason}
        if not alloc and not err and spec.queued(j):
            # Очередь без узла, который служит проекту, -- не нехватка мест
            # (#118). Узлы -- один раз на ростер и только если есть очередь.
            if placement is None:
                placement = _placement_nodes()
            # Потолок папета -- из его зарегистрированной спеки (#197): её и
            # размещает Nomad, а не ту, что собрал бы сегодняшний .mop.
            try:
                item["ceiling"] = spec.ceiling_of(nomad.get_job(j["ID"]))
            except Exception:
                item["ceiling"] = None
            item["unserved"] = spec.placement_gap(
                puppets.project_of((j.get("Meta") or {}).get("origin", "")), placement,
                item["ceiling"])
        if stale:
            # Полный джоб, а не заглушка из списка: врапер и ограничение
            # размещения лежат в спеке, а её get_jobs не отдаёт.
            try:
                item["stale"] = spec.spec_is_stale(nomad.get_job(j["ID"]))
            except Exception:
                item["stale"] = False
        items.append(item)
    return items


def _placement_nodes():
    """Узлы пула для unserved(): состояние из nomad_pool, meta -- по узлу.
    Только на сервере."""
    metas = nomad.nodes_meta()
    return [dict(n, meta=metas.get(n["name"], {})) for n in nomad_pool()]


def next_name(project):
    prefix = puppets.JOB_PREFIX
    taken = {j["ID"] for j in nomad.client().jobs.get_jobs(prefix=f"{prefix}{project}-")}
    n = 1
    while f"{prefix}{project}-{n}" in taken:
        n += 1
    return f"{prefix}{project}-{n}"


def nomad_pool():
    """Узлы пула как данные: [{name, status, free_mb, total_mb, slots, error}].

    Только датацентр пула: джобы папетов объявляют его, и планировщик на узлы
    других dc не смотрит вовсе. Показать такой узел свободными слотами —
    пообещать то, чего планировщик не даст: управляляющая машина в control
    однажды так светилась тремя слотами, пока два папета стояли в queued."""
    out = []
    for n in nomad.client().nodes.get_nodes():
        if n.get("Datacenter") != nomad.POOL_DC:
            continue
        if n["Status"] != "ready":
            out.append({"name": n["Name"], "status": n["Status"]})
            continue
        try:
            free, total = nomad.node_capacity(n)
            out.append({"name": n["Name"], "status": "ready", "free_mb": free,
                        "total_mb": total, "slots": free // spec.MEM,
                        # Закрытый для планирования узел остаётся ready и место
                        # на нём показывает честно, но ставить туда Nomad не
                        # станет — и раздавать креды туда незачем.
                        "eligible": n.get("SchedulingEligibility") != "ineligible"})
        except Exception as e:
            out.append({"name": n["Name"], "status": "ready",
                        "error": nomad.describe_error(e)})
    return out


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


def store_workspace(root, name, req):
    """workspace папета из запроса add/update (#133). Текст -- положить,
    пусто -- снять (в рабочей копии файла нет), нет поля -- не трогать:
    так перерегистрация без рабочей копии не стирает положенное."""
    if "workspace" in req:
        bootstrap.store(root, name, req["workspace"] or "")


# ─── ворота владения (#40) ───────────────────────────────────────────────
# Кто ведёт задание папета, знает только его клон (`.git/mop-owner`, #161),
# а клон -- на узле. Сервис спрашивает агента ДО изменения в Nomad: рестарт,
# останов, перерегистрация и снос чужого папета убивали работу другого
# мастера посреди тикета, и сверял владельца один send.
GATE_TIMEOUT = 15


def _clone_of(name):
    """Факты клона от агента узла, где стоит папет.
    -> None (аллокации нет) | {"clone": ...} | {"error": ...}."""
    alloc = nomad.latest_alloc(name)
    if not alloc or not alloc.get("NodeName"):
        return None
    try:
        return bus.request(alloc["NodeName"], "clone", name=name,
                           timeout=GATE_TIMEOUT, project=bus.ADMIN)
    except bus.BusError as e:
        return {"error": str(e)}


def gate(name, req, facts, now):
    """Пускать ли изменяющий глагол проекта. -> (отказ|None, заметка|None).

    facts -- ответ _clone_of. Аллокации нет -- спросить некого и убивать
    нечего: проходит. Агент не ответил -- «не знаю» не значит «ничей»
    (AGENT SILENT: папет может работать): отказ с причиной, force его
    снимает. Остальное решает lease.may_touch, как у агента."""
    force = bool(req.get("force"))
    if facts and facts.get("error"):
        if force:
            return None, f"owner unknown: {facts['error']}"
        return (f"{name}: cannot tell who leads it — {facts['error']}; "
                f"repeat with force if you know it is free"), None
    clone = (facts or {}).get("clone")
    owner = Owner.from_dict((clone or {}).get("owner"))
    ok, note = lease.may_touch(owner, req.get("owner"), clone, now, force)
    return (None, note) if ok else (f"{name}: {note}", None)


def _gated(fn):
    """Обработчик за воротами владения. Оператор (субъект admin) проходит,
    не спрашивая агента: gc, doctor и sweep не должны упираться в аренду и
    в молчащий узел."""
    def run(project, req):
        note = None
        if project != bus.ADMIN:
            why, note = gate(req["name"], req, _clone_of(req["name"]), time.time())
            if why:
                return {"error": why}
        out = fn(project, req)
        if note and not out.get("error"):
            out = dict(out, owner_note=note)
        return out
    run.__name__ = fn.__name__
    return run


def _add(project, req):
    origin = req.get("origin")
    # Лимит -- проекта из origin, а не просителя: оператор через admin
    # заводит папета тому же проекту и упирается в тот же потолок. Файл
    # читается на каждый запрос: правка лимита доезжает без рестарта.
    target = Project.of(origin, projects.read_limits())
    why = over_limit(target.name, _live_count(target.name), target.limit)
    if why:
        return {"error": why}
    name = next_name(target.name)
    # workspace -- до регистрации: первый подъём обязан его увидеть.
    store_workspace(bootstrap.ROOT, name, req)
    nomad.register(spec.job_spec(name, origin, req.get("profile")))
    return {"ok": True, "name": name, "origin": origin}


def _update(project, req):
    # Спеку собирает сервер: `register(spec)` глаголом не бывает, иначе
    # проситель кладёт на узел что хочет (докстринг модуля).
    name = req["name"]
    store_workspace(bootstrap.ROOT, name, req)
    nomad.register(spec.job_spec(name, req.get("origin"), req.get("profile"),
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
    # Снятый насовсем папет уносит свой workspace (#133); рецикл (purge=False)
    # его сохраняет -- update следом положит свежий.
    if req.get("purge", True):
        bootstrap.store(bootstrap.ROOT, req["name"], "")
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
    slim = {k: alloc.get(k) for k in ALLOC_FIELDS}
    # Падает ли задача и почему (#126): `mop attach` и `mop add` говорят это
    # вместо «not running» и двух минут ожидания.
    slim["task"], slim["reason"] = task_and_reason(alloc)
    meta = nomad.node_meta(alloc["NodeName"]) or {}
    return {"ok": True, "alloc": slim, "driver": meta.get("mop_driver")}


def _spec(project, req):
    job = nomad.get_job(req["name"])
    if not job:
        return {"error": f"no job {req['name']}"}
    return {"ok": True, "meta": job.get("Meta") or {},
            "status": job.get("Status"), "stale": spec.spec_is_stale(job)}


def _roster(project, req):
    """Ростер глазами проекта: джоб плюс его аллокация. Отбор — тот же
    visible(): непомеченные видит только оператор, и это не два правила, а
    одно. Аллокация едет вместе с джобом — клиенту иначе пришлось бы идти за
    каждой отдельным запросом по сети.

    stale=True добавляет вердикт об устаревшей спеке, и только по просьбе: он
    стоит вызова API на каждый джоб, а нужен одному `mop doctor`. Платить за
    него в каждом `mop list` было бы платой за чужой глагол."""
    return {"ok": True, "items": nomad_items(project, stale=bool(req.get("stale")))}


def _pool(project, req):
    """Ёмкость узлов пула. Проекту это положено: по свободным слотам мастер
    решает, заводить ли папета. Кто ещё живёт на узле — глагол `nodes`."""
    return {"ok": True, "nodes": nomad_pool()}


def _nodes(project, req):
    return {"ok": True, "nodes": nodes.nomad_rows(nomad_pool())}


def _drain(project, req):
    nomad.node_drain(req["node"], int(req.get("deadline") or 300))
    return {"ok": True, "node": req["node"]}


def _up(project, req):
    nomad.node_eligibility(req["node"], True)
    return {"ok": True, "node": req["node"]}


# Хосты инвентаря контроллера (#178): кладёт `mop deploy` (роль cluster),
# инвентарь сам лежит у контроллера, куда учётке пула хода нет.
INVENTORY_HOSTS = os.path.expanduser("~/.config/mop/inventory-hosts.json")


def inventory_refusal(node, hosts):
    """Почему узел нельзя забыть по инвентарю; None -- можно (#178).

    Узел, оставшийся в инвентаре, следующий `mop deploy` молча ставит снова,
    и forget оказывается не решением, а паузой до прогона. hosts=None --
    списка нет (сервер развёрнут до #178): не отказ, иначе оператор упёрся
    бы в файл, которого ему никто не клал."""
    if hosts is not None and node in hosts:
        return (f"{node} is still in the inventory — take it out and run "
                f"mop deploy first, or the next deploy configures it again")
    return None


def _inventory_hosts():
    """Список хостов инвентаря из файла deploy; None, если файла нет."""
    try:
        with open(INVENTORY_HOSTS) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _forget(project, req):
    node = req["node"]
    # Инвентарь -- до Nomad: отказ не должен стоить ни одного вызова API.
    why = inventory_refusal(node, _inventory_hosts())
    if why:
        return {"error": why}
    # Предохранитель читает сводку узла (статус, планирование), а не имя (#196).
    summary = nomad.node_summary(node)
    if summary is None:
        return {"error": f"no node {node} in the cluster"}
    why = nomad.forget_refusal(summary, nomad.node_allocs(node))
    if why:
        return {"error": why}
    nomad.node_forget(node)
    return {"ok": True, "node": node}


def _meta(project, req):
    nomad.set_node_meta(req["node"], req.get("updates") or {})
    return {"ok": True, "node": req["node"], "meta": nomad.node_meta(req["node"])}


# ─── глаголы: проекты (#117) ─────────────────────────────────────────────
# Реестр, лимиты и пароли папетов живут здесь, на сервере, и пишет их только
# сервис. Глаголы идут в потоках петли, поэтому правка реестра -- под замком:
# два одновременных завода иначе потеряли бы один из проектов.
_projects_lock = threading.Lock()


def _names(lines):
    return projects.names(*puppets.project_ids(lines))


def _users_apply(names, verify_user=None):
    """Файл пользователей шины по реестру, reload, проверка подключением.
    Не вышло -- исключение; откат делает вызывающий."""
    changed, pw = natsconf.apply(names, bootstrap.PUPPET_CREDS)
    if changed:
        natsconf.reload()
    # Проверяем всегда, а не только при изменении: SIGHUP о битом конфиге не
    # сообщает, и «файл уже такой» не значит «шина его приняла».
    if verify_user:
        bus.can_login(creds.puppet_user(verify_user), pw[verify_user],
                      config.get("MOP_NATS_PORT"))
    else:
        bus.can_login(busnames.SERVICE, natsconf.read_base()[busnames.SERVICE],
                      config.get("MOP_NATS_PORT"))
    return changed


def _with_rollback(lines, change):
    """Правка реестра с откатом: реестр и файл пользователей возвращаются,
    если шина нового пользователя не пустила."""
    users = natsconf.read_users()
    try:
        return change()
    except Exception:
        projects.write(lines)
        if users is not None:
            natsconf.write(users)
            try:
                natsconf.reload()
            except Exception:
                pass
        raise


def _projects(project, req):
    lines = projects.read()
    origins, legacy = puppets.project_ids(lines)
    return {"ok": True, "names": projects.names(origins, legacy),
            "origins": sorted(origins), "lines": sorted(lines),
            "limits": projects.read_limits()}


def _project_add(project, req):
    origin = req["origin"]
    with _projects_lock:
        lines = projects.read()
        new, added = projects.with_origin(origin, lines)
        name = puppets.project_of(origin)

        def change():
            projects.write(new)
            _users_apply(_names(new), verify_user=name)
        _with_rollback(lines, change)
    return {"ok": True, "name": name, "added": added}


def _project_delete(project, req):
    name = req["name"]
    with _projects_lock:
        lines = projects.read()
        new, dropped = projects.without_project(name, lines)
        alive = [j["ID"] for j in nomad_jobs(bus.ADMIN)
                 if puppets.project_of((j.get("Meta") or {}).get("origin", "")) == name]
        why = delete_refusal(name, dropped, alive)
        if why:
            return {"error": why}

        def change():
            projects.write(new)
            _users_apply(_names(new))
        _with_rollback(lines, change)
        # Лимит снятого проекта (#107) уходит с ним: заведённый заново проект
        # получил бы чужой потолок из прошлого. Пароль -- тоже: сервер раздавал
        # бы кред пользователя, которого на шине уже нет.
        limits = projects.read_limits()
        if name in limits:
            projects.write_limits(projects.with_limit(limits, name, None))
        try:
            os.remove(os.path.join(bootstrap.PUPPET_CREDS, creds.puppet_pass_file(name)))
        except FileNotFoundError:
            pass
        # Секреты снятого проекта (#127): заведённый заново получил бы чужие.
        import shutil
        shutil.rmtree(project_secrets.project_dir(project_secrets.ROOT, name),
                      ignore_errors=True)
    return {"ok": True, "name": name, "dropped": dropped}


def _project_limit(project, req):
    name = req["name"]
    value = req.get("value")
    if value is not None:
        value = projects.parse_limit(str(value))
    with _projects_lock:
        if name not in _names(projects.read()):
            return {"error": f"no project {name} in the registry; what the pool "
                             f"serves: mop project list"}
        projects.write_limits(projects.with_limit(projects.read_limits(), name, value))
    return {"ok": True, "name": name, "limit": value}


# ─── глаголы: секреты проекта (#127) ─────────────────────────────────────
def _registered(project):
    """Отказ по незаведённому проекту: опечатка в имени завела бы секреты
    проекту, которого нет."""
    if project not in _names(projects.read()):
        return {"error": f"no project {project} in the registry: mop project add"}
    return None


def _secret_put(project, req):
    why = _registered(project)
    if why:
        return why
    root = project_secrets.ROOT
    if req.get("kind") == "file":
        data = base64.b64decode(req["data"])
        return {"ok": True, "name": project_secrets.put_file(root, project, req["name"], data)}
    if req.get("kind") == "var":
        key, value = project_secrets.parse_var(f"{req['key']}={req['value']}")
        project_secrets.set_var(root, project, key, value)
        return {"ok": True, "name": key}
    return {"error": "secret_put: kind is file or var"}


def _secret_list(project, req):
    root = project_secrets.ROOT
    return {"ok": True, "files": project_secrets.list_files(root, project),
            "vars": project_secrets.list_vars(root, project)}


def _secret_remove(project, req):
    root = project_secrets.ROOT
    kind, name = req.get("kind"), req.get("name") or ""
    if kind == "file":
        gone = project_secrets.remove_file(root, project, name)
    elif kind == "var":
        gone = project_secrets.remove_var(root, project, name)
    else:
        return {"error": "secret_remove: kind is file or var"}
    if not gone:
        return {"error": f"no secret {kind} {name} in project {project}"}
    return {"ok": True}


# ─── глагол: токен посадки (#42) ─────────────────────────────────────────
# Take -- проверка и запись одним шагом: глаголы идут в потоках петли, и два
# мастера, прочтя «свободен» оба, иначе взяли бы токен оба.
_landing_lock = threading.Lock()


def _landing(project, req):
    """take/give/show токена посадки проекта. Проект -- из субъекта; у
    оператора (admin) токена нет, он называет проект полем."""
    if project == bus.ADMIN:
        project = req.get("project")
        if not project:
            return {"error": "landing: the token is a project's — name the project"}
    action = req.get("action") or "show"
    if action == "show":
        return {"ok": True, "project": project, "token": landing.read().get(project)}
    if action not in ("take", "give"):
        return {"error": f"landing: no such action {action}; take, give or show"}
    who = req.get("holder")
    if not who:
        return {"error": "landing: no holder — the token is taken by a person's "
                         "login on the bus"}
    force = bool(req.get("force"))
    with _landing_lock:
        tokens = landing.read()
        if action == "take":
            new, got = landing.take(tokens, project, who, req["puppet"],
                                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                    force=force)
        else:
            new, got = landing.give(tokens, project, who, force=force)
        if new != tokens:
            landing.write(new)
    return dict(got, project=project)


# ─── права: одна таблица (#173) ──────────────────────────────────────────
# Раньше -- пять параллельных наборов и HANDLERS: новый глагол правился в
# нескольких местах, и право расходилось молча. Порядок строк -- прежний
# VERBS: из него выводятся наборы и перечень в отказе на неизвестный глагол.
#
# PROJECT -- про собственных папетов проекта; оператору тоже.
# SECRET -- секреты проекта (#127): проект -- из субъекта, оператору нет.
# ADMIN -- про машины: место на узле общее для всех его жильцов, а увод
# папетов с машины касается всех проектов разом -- мастеру не показываем.
# Проекты (#117) -- тоже оператору: завод проекта заводит пользователя на
# шине, а лимит мастер поднял бы себе сам.
#
# named -- глагол называет джоб: у него проверяется владелец.
# acting -- из них те, что ДЕЛАЮТ: им отсутствие джоба -- отказ. Читающему
# `alloc` нет: `puppets.delete` спрашивает аллокацию УЖЕ СНЯТОГО джоба,
# дожидаясь, пока тот перестанет быть running, и отказ там оставлял тело
# работать сиротой (#89). `spec` в их числе: на его отказе стоит `lib.guard`.
# _gated -- изменяющие папета: за воротами владения (#40).
VERBS = {
    "ping":           Verb(_ping,           PROJECT, False, False),
    "roster":         Verb(_roster,         PROJECT, False, False),
    "pool":           Verb(_pool,           PROJECT, False, False),
    "add":            Verb(_add,            PROJECT, False, False),
    "update":         Verb(_gated(_update), PROJECT, True,  True),
    "restart":        Verb(_gated(_restart), PROJECT, True,  True),
    "stop":           Verb(_gated(_stop), PROJECT, True,  True),
    "delete":         Verb(_gated(_delete), PROJECT, True,  True),
    "alloc":          Verb(_alloc,          PROJECT, True,  False),
    "spec":           Verb(_spec,           PROJECT, True,  True),
    "secret_put":     Verb(_secret_put,     SECRET,  False, False),
    "secret_list":    Verb(_secret_list,    SECRET,  False, False),
    "secret_remove":  Verb(_secret_remove,  SECRET,  False, False),
    "landing":        Verb(_landing,        PROJECT, False, False),
    "nodes":          Verb(_nodes,          ADMIN,   False, False),
    "drain":          Verb(_drain,          ADMIN,   False, False),
    "up":             Verb(_up,             ADMIN,   False, False),
    "forget":         Verb(_forget,         ADMIN,   False, False),
    "meta":           Verb(_meta,           ADMIN,   False, False),
    "projects":       Verb(_projects,       ADMIN,   False, False),
    "project_add":    Verb(_project_add,    ADMIN,   False, False),
    "project_delete": Verb(_project_delete, ADMIN,   False, False),
    "project_limit":  Verb(_project_limit,  ADMIN,   False, False),
}
# Прежние наборы -- выводом из таблицы.
PROJECT_VERBS = tuple(v for v, d in VERBS.items() if d.scope in (PROJECT, SECRET))
SECRET_VERBS = tuple(v for v, d in VERBS.items() if d.scope == SECRET)
ADMIN_VERBS = tuple(v for v, d in VERBS.items() if d.scope == ADMIN)
NAMED_VERBS = tuple(v for v, d in VERBS.items() if d.named)
ACTING_VERBS = tuple(v for v, d in VERBS.items() if d.acting)


def answer(project, req):
    """Запрос от имени проекта -> ответ (dict). Синхронно: зовут в потоке.

    Владельца джоба узнаём ДО проверки и проверяем по нему, а не по имени:
    имя `pu-<проект>-<n>` собирается из того же basename, но правду о том,
    чей это папет, несёт только Meta.origin."""
    verb = req.get("verb")
    name = req.get("name")
    spec = _row(verb)
    origin, exists = (None, False)
    if spec and spec.named and name:
        origin, exists = _owner(name)
    why = refusal(project, verb, origin=origin or req.get("origin"), name=name,
                  job_exists=exists, new_origin=req.get("new_origin"))
    if why:
        return {"error": why}
    if spec.acting and name and not exists:
        return {"error": f"no job {name} in the cluster"}
    try:
        return spec.fn(project, req)
    except KeyError as e:
        return {"error": f"{verb}: missing field {e}"}
    except ValueError as e:
        # Отказ по содержимому запроса (имя, размер, переменная), а не Nomad.
        return {"error": f"{verb}: {e}"}
    except Exception as e:
        return {"error": f"{verb}: {nomad.describe_error(e)}"}


# ─── подписчик ───────────────────────────────────────────────────────────
def journal(project, req, out):
    """Строки журнала на один ответ."""
    who = (req.get("name") or req.get("node") or req.get("origin")
           or req.get("puppet") or "")
    return [f"{project}.{req.get('verb')} {who}: {out.get('error') or 'ok'}"]


def banner(subject, nomad_addr):
    return f"mop-cluster: subscribed to {subject}, Nomad at {nomad_addr}"


async def serve(log):
    """Подписчик сервера. Креды — оператора (admin): сервис слушает все
    проекты, а разделяет их проверкой проекта из субъекта."""
    subj = bus.cluster_subject(busnames.ANY)
    await service.serve("mop-cluster", subj, lambda project, req, _send: answer(project, req),
                        log, journal, lambda: banner(subj, nomad.ADDR))
