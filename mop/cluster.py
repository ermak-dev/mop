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
import os
import threading

import base64

from . import (bootstrap, bus, busnames, config, creds, natsconf, nodes, nomad,
               project_secrets, projects, puppets, service)

# Токен субъекта. Не "server": туда пишет узел, см. докстринг модуля.
CHANNEL = "cluster"

# Глаголы проекта: про его собственных папетов.
PROJECT_VERBS = ("ping", "roster", "pool", "add", "update", "restart", "stop",
                 "delete", "alloc", "spec",
                 # Секреты проекта (#127): проект -- из субъекта.
                 "secret_put", "secret_list", "secret_remove")
SECRET_VERBS = ("secret_put", "secret_list", "secret_remove")
# Глаголы оператора: про машины. Место на узле общее для всех его жильцов, а
# увод папетов с машины касается всех проектов разом — мастеру не показываем.
# Проекты (#117) -- тоже оператору: завод проекта заводит пользователя на
# шине, а лимит мастер поднял бы себе сам.
ADMIN_VERBS = ("nodes", "drain", "up", "forget", "meta",
               "projects", "project_add", "project_delete", "project_limit")
VERBS = PROJECT_VERBS + ADMIN_VERBS
# Глаголы, которые называют джоб: у них проверяется владелец.
NAMED_VERBS = ("update", "restart", "stop", "delete", "alloc", "spec")
# Из них те, что ДЕЛАЮТ: им отсутствие джоба — отказ. Читающему `alloc` нет:
# `puppets.delete` спрашивает аллокацию УЖЕ СНЯТОГО джоба, дожидаясь, пока
# тот перестанет быть running, и отказ там оставлял тело работать сиротой
# (#89). `spec` в список входит: на его отказе стоит `lib.require_job`.
ACTING_VERBS = ("update", "restart", "stop", "delete", "spec")


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
    if verb not in VERBS:
        return (f"no such verb {verb}; project verbs: {', '.join(PROJECT_VERBS)}; "
                f"operator verbs: {', '.join(ADMIN_VERBS)}")
    operator = project == bus.ADMIN
    if verb in SECRET_VERBS and operator:
        return (f"{verb}: secrets belong to a project — ask on "
                f"mop.<project>.cluster.rpc, not the operator's subject")
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
    # workspace -- до регистрации: первый подъём обязан его увидеть.
    store_workspace(bootstrap.ROOT, name, req)
    nomad.register(puppets.job_spec(name, origin, req.get("profile")))
    return {"ok": True, "name": name, "origin": origin}


def _update(project, req):
    # Спеку собирает сервер: `register(spec)` глаголом не бывает, иначе
    # проситель кладёт на узел что хочет (докстринг модуля).
    name = req["name"]
    store_workspace(bootstrap.ROOT, name, req)
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
    slim = {k: alloc.get(k) for k in puppets.ALLOC_FIELDS}
    # Падает ли задача и почему (#126): `mop attach` и `mop add` говорят это
    # вместо «not running» и двух минут ожидания.
    slim["task"], slim["reason"] = puppets.task_and_reason(alloc)
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
        alive = [j["ID"] for j in puppets.nomad_jobs(bus.ADMIN)
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


HANDLERS = {"ping": _ping, "roster": _roster, "pool": _pool, "add": _add, "update": _update, "restart": _restart,
            "stop": _stop, "delete": _delete, "alloc": _alloc, "spec": _spec,
            "nodes": _nodes, "drain": _drain, "up": _up, "forget": _forget,
            "meta": _meta, "projects": _projects, "project_add": _project_add,
            "project_delete": _project_delete, "project_limit": _project_limit,
            "secret_put": _secret_put, "secret_list": _secret_list,
            "secret_remove": _secret_remove}


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
    except ValueError as e:
        # Отказ по содержимому запроса (имя, размер, переменная), а не Nomad.
        return {"error": f"{verb}: {e}"}
    except Exception as e:
        return {"error": f"{verb}: {nomad.describe_error(e)}"}


# ─── подписчик ───────────────────────────────────────────────────────────
def journal(project, req, out):
    """Строки журнала на один ответ."""
    who = req.get("name") or req.get("node") or req.get("origin") or ""
    return [f"{project}.{req.get('verb')} {who}: {out.get('error') or 'ok'}"]


def banner(subject, nomad_addr):
    return f"mop-cluster: subscribed to {subject}, Nomad at {nomad_addr}"


async def serve(log):
    """Подписчик сервера. Креды — оператора (admin): сервис слушает все
    проекты, а разделяет их проверкой проекта из субъекта."""
    subj = bus.cluster_subject(busnames.ANY)
    await service.serve("mop-cluster", subj, lambda project, req, _send: answer(project, req),
                        log, journal, lambda: banner(subj, nomad.ADDR))
