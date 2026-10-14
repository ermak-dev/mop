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

Спеку джоба собирает сервер, а не проситель. Глагол — `add(origin)`,
не `register(spec)`: приняв готовую спеку, сервис отдал бы исполнение кода на
узлах любому, кто дотянулся до шины, и токен на сервере не защищал бы ничего.

Подписчик — mop-cluster, юнит на сервере; проверяется `mop server cluster check`.
Чистая часть (права) — tests/cluster.py.
"""
import contextlib
import contextvars
import json
import os
import threading
import time

import base64

from .. import driver
from . import bootstrap, grants, natsconf, nodes, nomad, proxykey, spec
from ..common import (bus, busnames, config, creds, domain, fsutil, landing, lease, llm, paths,
                      project_secrets, projects, puppets, service, soul, state)
from ..common.domain import Alloc, CloneFacts, JobMeta, PoolNode, Project, Verb

# Nomad за интерфейсом (#275): глаголы читают его через _api(), а не модуль
# nomad напрямую. По умолчанию -- живой модуль; проверки дают свою
# реализацию NomadApi через using() или answer(api=...). Контекстная
# переменная, а не глобальная: ответы идут в потоках сервиса.
_API = contextvars.ContextVar("nomad_api", default=nomad)


def _api():
    return _API.get()


@contextlib.contextmanager
def using(api):
    """Глаголы внутри блока ходят в Nomad через api (nomad.NomadApi)."""
    token = _API.set(api)
    try:
        yield api
    finally:
        _API.reset(token)


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
        # new_origin сюда кладёт _answer, сравнив origin запроса с джобом
        # (#365), а не клиент.
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
# Аллокацию возим значением Alloc (#377) -- те поля, что читает клиент, а не
# аллокацию целиком: в ней TaskStates и события, и тринадцать таких ответов
# упираются в предел сообщения шины на пустом месте.


def nomad_jobs(project=None):
    """Джобы папетов из Nomad. Зовёт это только сервис кластера: он один
    говорит с Nomad от чужого имени (docs/CLUSTER.md)."""
    listing = _api().get_jobs(puppets.JOB_PREFIX, meta=True)
    return puppets.visible(listing, project)


def task_and_reason(alloc):
    """Сводка задачи и, у падающей, причина из stderr (#126). Только сервис
    кластера: у него Nomad. stderr читается лишь у падающих -- это запрос к
    клиенту Nomad на узле, и платить его за здоровых незачем."""
    task = state.task_summary(alloc)
    reason = None
    if state.failing_row(Alloc.from_dict(alloc).client_status, task, None):
        try:
            reason = state.failure_reason(
                _api().alloc_stderr(Alloc.from_dict(alloc).id, state.task_name(alloc)))
        except Exception:
            pass
    return task, reason


def _with_soul(j):
    """Джоб с проекцией роли (#582, для autopick #585): MopSoul.
    receives_dispatch читается из ЗАКРЕПЛЁННОГО бандла -- не из
    самодекларации джоба: Meta говорит, КТО это, бандл -- ЧТО ему можно.
    Бандл потерян -- поля нет: autopick такого не выбирает (fail closed),
    легаси-джоб -- тоже без поля."""
    try:
        got = spec.pinned_bundle(j["Meta"].get("soul"),
                                 j["Meta"].get("soul_digest"))
        if got is not None:
            return {**j, "MopSoul": {
                "receives_dispatch": got.manifest.runtime.receives_dispatch}}
    except Exception:
        pass
    return j


def nomad_items(project=None, stale=False):
    """Ростер из Nomad: джоб плюс его аллокация. Тоже только сервис кластера.

    Аллокация приезжает вместе с джобом намеренно: клиент за ней отдельно уже
    не сходит, а N+1 запрос по шине вместо N+1 вызова API — та же цена, но с
    сетью между ними."""
    items, placement = [], None
    for j in sorted(nomad_jobs(project), key=lambda j: j["ID"]):
        alloc, err, task, reason = None, None, None, None
        try:
            got = _api().latest_alloc(j["ID"])
            alloc = Alloc.from_dict(got).to_dict() if got else None
            if got:
                task, reason = task_and_reason(got)
        except Exception as e:
            err = nomad.describe_error(e)
        item = {"job": _with_soul(j), "alloc": alloc, "error": err,
                "task": task, "reason": reason}
        # Сдавшийся bootstrap (#345): джоб остановлен, аллокации может не
        # быть вовсе -- причина из записи итога на этом же сервере.
        gave_up = bootstrap.gave_up_of(bootstrap.ROOT, j["ID"])
        if gave_up:
            item["gave_up"] = gave_up
        if not alloc and not err and state.queued(j):
            # Очередь без узла, который служит проекту, -- не нехватка мест
            # (#118). Узлы -- один раз на ростер и только если есть очередь.
            if placement is None:
                placement = _placement_nodes()
            # Потолок папета -- из его зарегистрированной спеки (#197): её и
            # размещает Nomad, а не ту, что собрал бы сегодняшний .mop.
            try:
                item["ceiling"] = spec.ceiling_of(_api().get_job(j["ID"]))
            except Exception:
                item["ceiling"] = None
            item["unserved"] = spec.placement_gap(
                JobMeta.from_job(j).project, placement,
                item["ceiling"])
        if stale:
            # Полный джоб, а не заглушка из списка: врапер и ограничение
            # размещения лежат в спеке, а её get_jobs не отдаёт.
            try:
                item["stale"] = spec.spec_is_stale(_api().get_job(j["ID"]))
            except Exception:
                item["stale"] = False
        items.append(item)
    return items


def _placement_nodes():
    """Узлы пула для unserved(): состояние из nomad_pool, meta -- по узлу.
    Только на сервере."""
    metas = _api().nodes_meta()
    return [dict(n, meta=metas.get(n["name"], {})) for n in nomad_pool()]


def _reserved_names(project):
    """Имена, занятые резервами грантов ДО Nomad (#582). -> {job}.

    Nomad не видит подвисшего резерва (prepare доехал, регистрация нет),
    и next_name по одним его джобам отдал бы это имя второй заявке:
    ретрай первого затёр бы имя и закреплённую политику. Реестр -- та же
    правда об имени, что и Nomad; отозванный резерв имени не держит --
    его принципал мёртв, имя свободно. Битый реестр -- громкий отказ
    _read: тихий пустой список здесь означал бы «Nomad единственная
    правда» именно в тот момент, когда он правдой быть перестал."""
    reg = grants._read(SOUL_ROOT)
    return {rec.get("job") for rec in reg.values()
            if rec.get("project") == project and rec.get("job")
            and not rec.get("revoked_at")}


def next_name(project, reserving=False):
    """Следующее свободное имя папета проекта. reserving=True -- заявка
    роли: имя обязано не пересекаться и с резервами грантов (#582),
    не только с джобами Nomad."""
    prefix = puppets.JOB_PREFIX
    taken = {j["ID"] for j in _api().get_jobs(f"{prefix}{project}-")}
    if reserving:
        taken |= _reserved_names(project)
    n = 1
    while f"{prefix}{project}-{n}" in taken:
        n += 1
    return f"{prefix}{project}-{n}"


def nomad_pool():
    """Узлы пула как данные:
    [{name, status, free_mb, total_mb, slots, slots_total, error}].
    slots -- свободные, slots_total -- сколько папетов берёт пустой узел (#243).

    Только датацентр пула: джобы папетов объявляют его, и планировщик на узлы
    других dc не смотрит вовсе. Показать такой узел свободными слотами —
    пообещать то, чего планировщик не даст: управляляющая машина в control
    однажды так светилась тремя слотами, пока два папета стояли в queued."""
    out = []
    for n in _api().get_nodes():
        if n.get("Datacenter") != nomad.POOL_DC:
            continue
        if n["Status"] != "ready":
            out.append(PoolNode(n["Name"], n["Status"]).to_pool())
            continue
        try:
            free, total = _api().node_capacity(n)
            out.append(PoolNode(n["Name"], "ready", free_mb=free, total_mb=total,
                            slots=free // spec.MEM, slots_total=total // spec.MEM,
                            # Закрытый для планирования узел остаётся ready и
                            # место на нём показывает честно, но ставить туда
                            # Nomad не станет — и раздавать креды туда незачем.
                            eligible=n.get("SchedulingEligibility") != "ineligible").to_pool())
        except Exception as e:
            out.append(PoolNode(n["Name"], "ready", error=nomad.describe_error(e)).to_pool())
    return out


# ─── глаголы: Nomad ──────────────────────────────────────────────────────
def _owner(name):
    """(origin джоба, есть ли джоб). Разные исходы у «нет джоба» и «джоб без
    метки» — на них по-разному отвечает refusal()."""
    try:
        job = _api().get_job(name)
    except Exception:
        return None, False
    if not job:
        return None, False
    return (JobMeta.from_job(job).origin or None), True


def _ping(project, req):
    """Жив ли сервис и видит ли он Nomad. Отвечает всем: мастеру надо уметь
    отличить «сервис лёг» от «Nomad не отвечает», не имея ни того, ни другого
    под рукой."""
    try:
        seen = len(_api().get_nodes())
        cluster_ok, why = True, None
    except Exception as e:
        seen, cluster_ok, why = 0, False, nomad.describe_error(e)
    out = {"ok": True, "nomad": nomad.ADDR, "reachable": cluster_ok,
           "nodes": seen, "error": why, "project": project,
           "verbs": list(VERBS)}
    # Роль-протокол (#582) объявляется ТОЛЬКО включённым сервером: старый
    # или полусобранный молчит, и клиент зовёт легаси-путь, не надеясь на
    # request_id, которого тут нет.
    if config.get("MOP_SOUL_ROLES"):
        out["soul_protocol"] = {"version": soul.VERSION, "request_id": True}
    return out


def _live_count(target):
    """Сколько джобов проекта живо. По префиксу имени: имена `pu-<проект>-<n>`
    строятся из того же basename, что и проект (next_name)."""
    got = _api().get_jobs(f"{puppets.JOB_PREFIX}{target}-")
    return sum(1 for j in got if j.get("Status") != "dead")


def store_workspace(root, name, req):
    """workspace папета из запроса add/update (#133). Текст -- положить,
    пусто -- снять (в рабочей копии файла нет), нет поля -- не трогать:
    так перерегистрация без рабочей копии не стирает положенное.

    -> метка регистрации либо None (#334): с полем bootstrap_sent рядом
    кладётся происхождение, и итог прогона сверяется с этой меткой. Без поля
    (старый клиент) -- ничего нового."""
    if "workspace" not in req:
        return None
    bootstrap.store(root, name, req["workspace"] or "")
    if "bootstrap_sent" in req:
        return bootstrap.note_sent(root, name, req["bootstrap_sent"])
    return None


def _version(name):
    """Версия джоба в Nomad либо None: не прочиталась -- не знаем."""
    try:
        return (_api().get_job(name) or {}).get("Version")
    except Exception:
        return None


# ─── ворота владения (#40) ───────────────────────────────────────────────
# Кто ведёт задание папета, знает только его клон (`.git/mop-owner`, #161),
# а клон -- на узле. Сервис спрашивает агента ДО изменения в Nomad: рестарт,
# останов, перерегистрация и снос чужого папета убивали работу другого
# мастера посреди тикета, и сверял владельца один send.
GATE_TIMEOUT = 15


def _clone_of(name):
    """Факты клона от агента узла, где стоит папет.
    -> None (аллокации нет) | {"clone": ...} | {"error": ...}."""
    alloc = Alloc.from_dict(_api().latest_alloc(name))
    if not alloc or not alloc.node:
        return None
    try:
        return bus.request(alloc.node, "clone", name=name,
                           timeout=GATE_TIMEOUT, project=bus.ADMIN)
    except bus.BusError as e:
        return {"error": str(e)}


def gate(name, req, facts, now):
    """Пускать ли изменяющий глагол проекта. -> (отказ|None, заметка|None).

    facts -- ответ _clone_of. Аллокации нет -- спросить некого и убивать
    нечего: проходит. Агент не ответил -- «не знаю» не значит «ничей»
    (AGENT SILENT: папет может работать): отказ с причиной, force его
    снимает. Остальное решает lease.gate, как у агента (#267)."""
    force = bool(req.get("force"))
    if facts and facts.get("error"):
        if force:
            return None, f"owner unknown: {facts['error']}"
        return (f"{name}: cannot tell who leads it — {facts['error']}; "
                f"repeat with force if you know it is free"), None
    return lease.gate(name, CloneFacts.from_dict((facts or {}).get("clone")),
                      lease.caller(req)[0], now, force)


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
        return lease.noted(fn(project, req), note)
    run.__name__ = fn.__name__
    return run


# ─── роль: завод души на сервере (#582, docs/SOUL.md #579) ────────────────
# Корень реестра привязок -- тот же приватный каталог, что у grants (их
# дефолты равны: grants.STORE рядом с кредами папетов); свой провод, чтобы
# проверки подменяли одно место. Каталог ролей -- поставляемые пресеты
# пакета: имя роли клиент называет, бандл читает сервер.
SOUL_ROOT = os.path.join(os.path.dirname(bootstrap.PUPPET_CREDS), grants.STORE)
SOUL_CATALOGUE = (os.path.join(config.PROJECT, "mop", "skills"),)




class _Launch:
    """Завод роли одним объектом: бандл (каталог свежего запуска, хранилище
    резерва -- у ретрая, #582) плюс запись резерва ретрая."""

    def __init__(self, role, bundle, request_id, record=None):
        self.role = role
        self.bundle = bundle
        self.request_id = request_id
        self.record = record

    def intent(self, req):
        """Интент резерва (#581): цель запуска, ретрай сверяет по нему
        полезную нагрузку -- «похожий» запуск с другой веткой другой и
        есть. Workspace -- часть интента (#582, блокатор ревью): без него
        «тот же запрос» с другим текстом не различим вовсе, и конфликтный
        реплей затирал закреплённые файлы до отказа prepare."""
        ws = req.get("workspace")
        text = "" if ws is None else str(ws)
        return f"add {req.get('origin')}@{req.get('branch') or ''} ws={len(text)}:{self._ws_hash(text)}"

    @staticmethod
    def _ws_hash(text):
        import hashlib
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def project_of(self, req):
        """Проект заявки по origin -- тем же правилом, что и ворота."""
        return puppets.project_of(req.get("origin") or "")

    def triple(self, soul_inc):
        return {"soul": self.role, "soul_digest": self.bundle.digest,
                "soul_inc": soul_inc}


def _delegated(project, req, ctx):
    """Делегированный add по каналу роли (#582/#603): принципал
    поднимает ребёнка сам, сервер выбирает всё остальное. Границы:

    - авторизация -- УЖЕ швом authorize_request (#581) в _answer: субъект
      проверен busnames.cluster, глагол -- свежим resolve, контекст шва
      (actor/owner/job/spawn) -- единственная правда о родителе; здесь
      своих чтений реестра НЕТ: второе чтение -- вторая правда;
    - роль цели -- по списку spawn КОНТЕКСТА шва (fail closed);
    - клиентский workspace/bootstrap_sent -- отказ ДО всего: грант не
      грузит текст и не создаёт легаси-ребёнка без души (omitting soul --
      обход роли, не легаси-путь);
    - политика ребёнка -- унаследованный СОГЛАСОВАННЫЙ снапшот родителя
      (bootstrap.copy_approval, хранилище по имени папета #133),
      закреплённый резервом до Nomad: ретрай наследует то, что закрепила
      первая попытка, а не свежую политику;
    - происхождение -- actor в записи гранта, владелец остаётся
      человеком-хозяином родителя: актёр не открывает путь загрузки.

    -> ответ словарем; None здесь невозможен (ветка только add)."""
    caller = ctx["actor"]
    if req.get("verb") != "add":
        return {"error": f"{caller}: the role channel carries the add "
                         f"verb only in this protocol version (#582)"}
    if "workspace" in req or "bootstrap_sent" in req:
        return {"error": f"{caller}: a delegated add takes no client "
                         f"workspace text: the child inherits the parent's "
                         f"approved bootstrap (#582)"}
    # Граница проекта -- для всех актёров (#567): легаси-путь держит её
    # refusal(add), делегированная ветка идёт мимо -- тот же вопрос здесь.
    why = _foreign_origin(project, req.get("origin"))
    if why:
        return {"error": f"{caller}: {why}"}
    if req.get("soul") not in ctx["spawn"]:
        return {"error": f"{caller}: the role {req.get('soul')} is not in "
                         f"its parent's spawn allowlist "
                         f"({', '.join(ctx['spawn']) or 'none'}); spawn is "
                         f"the parent soul's, not the caller's to choose "
                         f"(#582)"}
    # Дальше -- путь _add с маркером делегирования: тот держит ворота
    # проекта, резерв и активацию, здесь только граница источника.
    # Родитель и хозяин -- из контекста шва: он читал реестр, мы -- нет.
    req = {**req, "_delegated_by": caller,
           "_parent_job": ctx["job"],
           "_owner": ctx["owner"] or project}
    return _add(project, req)


def _role_launch(req):
    """Путь роли глагола add (#582). -> None (легаси: полей роли нет) |
    _Launch | {"error": ...}. Источник бандла -- каталог сервера: клиент
    называет имя роли, digest и инкарнацию выбирает сервер, и попытка их
    подложить -- отказ до ворот проекта. Выключенная настройка закрывает
    путь роли целиком: полусобранный сервер протокол не рекламирует."""
    if not any(k in req for k in ("soul", "soul_digest", "soul_inc")):
        return None
    if not config.get("MOP_SOUL_ROLES"):
        return {"error": "soul roles are off on this server "
                         "(MOP_SOUL_ROLES empty): the legacy add only"}
    if "soul_digest" in req or "soul_inc" in req:
        return {"error": "add takes a role name, not a digest or an "
                         "incarnation: the server pins both (#579)"}
    if not req.get("request_id"):
        return {"error": "a role add needs a request_id: retries reconcile "
                         "the reserved principal instead of breeding a "
                         "second one (#579)"}
    name = req.get("soul")
    # Ретрай смотрит реестр ДО выбора имени/инкарнации (#581 by_request):
    # неопределённый исход прошлой попытки поднимает сохранённые, а не
    # заводит вторую душу. Дубль request_id в реестре -- отказ от grants.
    try:
        record = grants.by_request(req["request_id"], root=SOUL_ROOT)
    except ValueError as e:
        return {"error": f"soul add: {e}"}
    # Бандл ретрая -- ИЗ РЕЗЕРВА (#582, блокатор ревью): роль может уже
    # уйти из каталога, а бандл резерва живёт в хранилище write-once --
    # ретрай поднимает то, что закрепила первая попытка. Свежий запуск
    # бандл берёт из каталога: клиент называет имя, digest выбирает
    # сервер, и попытка подложить digest/inc -- отказ до ворот проекта.
    bundle = None
    if record is not None:
        if record.get("role") != name:
            return {"error": f"request {req['request_id']} is already "
                             f"prepared with a different role: "
                             f"{record.get('role')!r} reserved, {name!r} "
                             f"in this replay (#582)"}
        got = grants.bundle(record.get("soul_digest"), root=SOUL_ROOT)
        if got is not None:
            bundle = soul.from_manifest(got["manifest"], got["prompt"])
    if bundle is None:
        try:
            roles = soul.catalogue(SOUL_CATALOGUE)
            bundle = soul.load(roles[name]) if name in roles else None
        except ValueError as e:
            return {"error": f"soul catalogue: {e}"}
        if bundle is None:
            return {"error": f"no role {name} in the catalogue; available: "
                             f"{', '.join(sorted(roles)) or 'none'}"}
    return _Launch(name, bundle, req["request_id"], record)


def _replay_conflict(req, launch):
    """Почему реплей конфликтен с резервом (#582). -> отказ | None.

    Свой резерв (тот же request_id) -- та же полезная нагрузка или её
    нет вовсе: сверяются owner/project/actor/role и интент С workspace
    (--66: прежний интент origin@branch не связывал текст, и «тот же
    запрос» с другим текстом проходил до файлов). Чужой резерв на этом
    имени -- _reserved_taken. Вызов ДО side effects: конфликтный реплей
    не должен затирать закреплённые файлы до отказа prepare."""
    rec = launch.record
    if not rec:
        return None
    who, verified = lease.caller(req)
    delegated = req.get("_delegated_by")
    want = {"owner": rec.get("owner"),
            "project": rec.get("project"),
            "actor": rec.get("actor") or None,
            "role": rec.get("role"), "soul_digest": rec.get("soul_digest")}
    have = {"owner": (req.get("_owner") if delegated is not None else
                      (who if verified else None)),
            "project": launch.project_of(req), "actor": delegated,
            "role": launch.role, "soul_digest": launch.bundle.digest}
    for key in want:
        if want[key] != have[key]:
            return (f"request {launch.request_id} is already prepared with a "
                    f"different {key}: {want[key]!r} reserved, {have[key]!r} "
                    f"in this replay (#582)")
    want_intent = rec.get("intent") or ""
    have_intent = launch.intent(req)
    if want_intent != have_intent:
        return (f"request {launch.request_id} is already prepared with a "
                f"different intent: {want_intent!r} reserved, "
                f"{have_intent!r} in this replay (#582)")
    return None


def _reserved_taken(name, rec):
    """Занято ли имя резерва ЧУЖОЙ регистрацией (#582). -> отказ | None.
    Джоб с той же инкарнацией -- своя неопределённая регистрация: ретрай
    подтверждает её, а не отказывает; без души или с другой -- чужая."""
    if name not in {j["ID"] for j in
                    _api().get_jobs(f"{puppets.JOB_PREFIX}{rec.get('project')}-")}:
        return None
    if _kept_meta(name).soul_inc == rec.get("soul_inc"):
        return None
    return (f"{name}: the name reserved by {rec.get('request_id')} is taken "
            f"by another registration; the reservation needs the operator")


def _revoke_soul(project, name):
    """Отозвать привязку снятого папета (#582): active:false (#581) --
    активных кредов на несуществующий джоб нет. Пароль и бандл живут:
    реюз имени джоба -- всё равно новый грант."""
    rec = grants.by_job(project, name, root=SOUL_ROOT)
    if rec is not None:
        grants.revoke(rec["principal"], root=SOUL_ROOT)


def _add(project, req):
    origin = req.get("origin")
    # Роль (#582): add с душой резервирует имя, инкарнацию и принципала
    # ДО Nomad (grants.prepare) и подтверждает после (activate): отказ
    # Nomad оставляет резерв, ретрай того же request_id поднимает
    # сохранённого принципала, а не второго на то же имя.
    launch = _role_launch(req)
    if isinstance(launch, dict):
        return launch
    # Ворота проекта (#567): лимит → count → name → workspace → register --
    # одним куском, иначе два одновременных add выбирают одно имя, обходят
    # квоту, а смена лимита врезается между чтением и регистрацией. Имя
    # под ворота -- из origin тем же правилом, что и везде; реестр add не
    # пишет, реестровый замок здесь не нужен.
    with _project_gate(puppets.project_of(origin or "")):
        # Наличие проекта -- внутри ворот и до всего (#567): удаление и
        # завод врезаются в один проект, и add, стоявший у ворот, обязан
        # увидеть снятый проект, а не регистрировать джоба отсутствующему.
        # Раньше отсутствие читалось как unlimited -- дыра приёмки #567.
        names = projects.snapshot_names(_view())
        target_name = puppets.project_of(origin or "")
        if target_name not in names:
            return {"error": f"no project {target_name} in the registry; what the pool "
                             f"serves: mop project list"}
        # Лимит -- проекта из origin, а не просителя: оператор через admin
        # заводит папета тому же проекту и упирается в тот же потолок.
        # Реестр читается на каждый запрос: правка лимита доезжает без
        # рестарта. Читатель снимка (#534): взгляд читателя, пишет его
        # только глагол проектов -- непредставимая пара не прячет потолок
        # действующего.
        target = Project.of(origin, projects.snapshot_limits(_view()))
        # Реплей сверяется и выбирается ДО квоты (#582): успешный запуск,
        # занявший последний слот, обязан реплеиться без отказа -- квота
        # считает НОВЫХ папетов, а не подтверждение своего резерва.
        if launch is not None and launch.record:
            # Ретрай: имя и инкарнация -- из резерва, не новые (#579);
            # чужая регистрация на этом имени -- громкий отказ.
            name, soul_inc = launch.record["job"], launch.record["soul_inc"]
            why = _replay_conflict(req, launch)
            if why:
                return {"error": why}
            why = _reserved_taken(name, launch.record)
            if why:
                return {"error": why}
        else:
            why = over_limit(target.name, _live_count(target.name), target.limit)
            if why:
                return {"error": why}
            name = (next_name(target.name) if launch is None else
                   next_name(target.name, reserving=True))
            soul_inc = soul.incarnation() if launch is not None else None
        # workspace -- до регистрации: первый подъём обязан его увидеть.
        # Актёров различает не запрет, а путь (#582): ЧЕЛОВЕК, заводящий
        # роль, согласовывает политику своим текстом -- метка согласования
        # ставится тем же ходом, текст идёт в хранилище как сегодня; грант
        # (делегированный add) текста не приносит вовсе -- его ребёнок
        # наследует закреплённый снапшот родителя ниже, и попытка прислать
        # YAML отклонена в _delegated ДО ворот проекта.
        delegated = req.get("_delegated_by")
        marker = None
        if launch is None:
            marker = store_workspace(bootstrap.ROOT, name, req)
        elif delegated is None:
            # Новый грант -- только верифицированному человеку (#582):
            # анонимная заявка роли не заводит принципала. Легаси-add
            # (без роли) совместимость сохраняет -- его джобы не гранты.
            who, verified = lease.caller(req)
            if not verified:
                return {"error": "a role add needs a verified human caller: "
                                 "the login comes from the request subject, "
                                 "not the body (#582)"}
            if "workspace" in req:
                store_workspace(bootstrap.ROOT, name, req)
                bootstrap.note_approval(bootstrap.ROOT, name)
            elif "bootstrap_sent" in req:
                return {"error": "a role add carries workspace text, not a "
                                 "bare bootstrap_sent marker (#582)"}
        else:
            # Делегированный ребёнок: снапшот родителя под именем ребёнка
            # ДО Nomad -- ретрай наследует закреплённое, не свежее: копия
            # кладётся только когда резерва ещё не было (свежий запуск),
            # ретрай уже держит её под своим именем.
            if not (launch and launch.record):
                why = bootstrap.copy_approval(
                    bootstrap.ROOT, req.get("_parent_job") or "", name)
                if why:
                    return {"error": why}
        principal = None
        if launch is not None:
            # Резерв ДО Nomad: что бы ни случилось с регистрацией, принципал,
            # имя и инкарнация уже сохранены -- ретрай поднимет их. Тот же
            # request_id с другой полезной нагрузкой (#581) откажет здесь.
            # Владелец -- человек (делегированный: хозяин родителя из
            # реестра), актёр -- отдельным полем: происхождение не право.
            principal = grants.prepare(
                project, role=launch.role, digest=launch.bundle.digest,
                manifest=launch.bundle.manifest.to_dict(),
                prompt=launch.bundle.prompt,
                owner=(req.get("_owner") if delegated is not None else
                       (lease.caller(req)[0] or project)),
                request_id=launch.request_id, job=name, soul_inc=soul_inc,
                intent=launch.intent(req), root=SOUL_ROOT,
                actor=delegated)["principal"]
        _api().register(spec.job_spec(
            name, origin, branch=req.get("branch"),
            **(launch.triple(soul_inc) if launch is not None else {})))
        if principal is not None:
            grants.activate(principal, root=SOUL_ROOT)
    # Метка (#334) -- только когда клиент прислал происхождение: без неё
    # ответ байт в байт прежний. Роль (#582) эхом несёт тройку: клиент
    # неполного ответа сверяется, а не пересоздаёт.
    out = {"ok": True, "name": name, "origin": origin,
           **({"bootstrap_marker": marker} if marker else {})}
    if launch is not None:
        out.update(launch.triple(soul_inc))
    return out


def _kept_meta(name):
    """Мета джоба (#256): ветка, с которой папет заведён."""
    # Чтение по возможности: нет джоба или Nomad молчит -- нет и меты, а о
    # самом Nomad скажет register следом.
    try:
        job = _api().get_job(name)
    except Exception:
        return JobMeta(None, None)
    return JobMeta.from_job(job)


def _update(project, req):
    # Спеку собирает сервер: `register(spec)` глаголом не бывает, иначе
    # проситель кладёт на узел что хочет (докстринг модуля).
    name = req["name"]
    # Роль на месте не меняется (#582): чужое имя роли или подложенный
    # digest -- отказ до перерегистрации, смена роли -- новый папет, у
    # живой инкарнации свой бандл (#579). Работа при этом сохраняется --
    # в клоне.
    kept = _kept_meta(name)
    why = _role_frozen(req, kept)
    if why:
        return {"error": why}
    marker = store_workspace(bootstrap.ROOT, name, req)
    # Спека без изменений -- Nomad не переразмещает, прогона не будет (#334):
    # клиент должен услышать это, а не ждать итога до таймаута.
    before = _version(name) if marker else None
    # Ветка -- свойство папета (#257): запрос без неё (recycle, gc, лечение
    # doctor'а) не стирает ту, с которой папет заведён. Аренда кредита
    # (#284) -- так же, и профиль сверяется с реестром. Тройка роли (#582)
    # -- тем же правилом: respec переживает её, как ветку.
    branch = req.get("branch") or kept.branch
    # Узел -- из аллокации (#289): стоящий папет перерегистрируется на своём
    # узле, где его тело с клоном; неразмещённый (нет аллокации) -- куда
    # поставит Nomad, как при подъёме.
    alloc = Alloc.from_dict(_api().latest_alloc(name))
    node = (alloc.node if alloc else None) or None
    _api().register(spec.respec(name, JobMeta(req.get("origin"), branch,
                                               soul=kept.soul,
                                               soul_digest=kept.soul_digest,
                                               soul_inc=kept.soul_inc),
                               cont=bool(req.get("cont")), node=node))
    out = {"ok": True, "name": name, "node": node}
    if marker:
        out.update(bootstrap_marker=marker,
                   replaced=before is None or _version(name) != before)
    return out


def _bootstrap_result(project, req):
    """Итог последнего прогона bootstrap'а папета (#334): запись либо None.
    `mop add|update` ждут в ней метку своей регистрации."""
    return {"ok": True, "name": req["name"],
            "result": bootstrap.read_result(bootstrap.ROOT, req["name"])}


def _restart(project, req):
    alloc = Alloc.from_dict(_api().latest_alloc(req["name"]))
    if not alloc:
        return {"error": f"{req['name']} has no allocation to restart"}
    _api().alloc_restart(alloc.id)
    return {"ok": True, "alloc": alloc.id, "node": alloc.node}


def _stop(project, req):
    alloc = Alloc.from_dict(_api().latest_alloc(req["name"]))
    if not alloc:
        return {"error": f"{req['name']} has no allocation to stop"}
    _api().alloc_stop(alloc.id)
    return {"ok": True, "alloc": alloc.id}


def _role_frozen(req, kept):
    """Почему запрос update меняет роль живого папета. -> отказ | None.

    Явное имя роли -- допустимо только то же (#582): это тот же папет, а
    не перезапись души; digest и инкарнация клиенту не принадлежат вовсе
    (#579: их выбирает сервер)."""
    if kept.soul is None:
        if req.get("soul") or req.get("soul_digest") or req.get("soul_inc"):
            return ("update cannot attach a role: a soul puppet is a new "
                    "add, the work lives on in its clone (#579)")
        return None
    if req.get("soul") not in (None, kept.soul):
        return (f"update cannot change the role of a live incarnation: "
                f"{kept.soul} -> {req.get('soul')}; a new role is a new "
                f"puppet, and the work stays in its clone (#579)")
    if "soul_digest" in req or "soul_inc" in req:
        return ("update cannot rewrite the digest or the incarnation of a "
                "live puppet: a new bundle is a new puppet (#579)")
    return None


def _delete(project, req):
    """Снять джоб. purge=False оставляет его в истории остановленным — так
    работает рецикл: джоб останавливается, рабочая копия сносится, и та же
    спека поднимается обратно."""
    name = req["name"]
    _api().deregister(name, purge=bool(req.get("purge", True)))
    # Снятый насовсем папет уносит свой workspace (#133); рецикл (purge=False)
    # его сохраняет -- update следом положит свежий. Метка согласования
    # уходит вместе с файлами (#582): имени без папета нет политики.
    if req.get("purge", True):
        bootstrap.store(bootstrap.ROOT, name, "")
        bootstrap.drop_approval(bootstrap.ROOT, name)
    # Душа снятого уходит с ним (#582): глагольный отзыв (#581), активных
    # кредов на несуществующий джоб нет. Рецикл (purge=False) душу
    # сохраняет -- папет вернётся с той же инкарнацией.
    if req.get("purge", True):
        _revoke_soul(project, name)
    return {"ok": True, "name": name}


def _alloc(project, req):
    """Аллокация папета плюс драйвер её узла.

    Драйвер здесь, а не отдельным глаголом: `mop attach` спрашивает ровно эти
    две вещи вместе — где папет стоит и чем в него входят, — и второй запрос
    по сети ради одного поля меты был бы платой ни за что."""
    try:
        alloc = _api().latest_alloc(req["name"])
    except Exception as e:
        # Ошибка чтения -- не отсутствие (#572): подтверждённое «джоба нет»
        # уже превращает в None сам клиент (NotFound), сюда доезжает сбой --
        # сеть, лежащий API, чужая 500. Ответить «аллокации нет» -- сказать
        # достоверную ложь: puppets.delete забывает узел и сносит джоб без
        # cleanup, _wait_stopped принимает сбой за завершённый stop. Сбой
        # доезжает отказом с причиной (номадную половину опишет _answer).
        return {"error": f"alloc {req['name']}: {nomad.describe_error(e)}"}
    # Сдавшийся bootstrap (#345) -- ответом, с аллокацией или без.
    gave_up = bootstrap.gave_up_of(bootstrap.ROOT, req["name"])
    extra = {"gave_up": gave_up} if gave_up else {}
    if not alloc:
        return {"ok": True, "alloc": None, "driver": None, **extra}
    # Падает ли задача и почему (#126): `mop attach` и `mop add` говорят это
    # вместо «not running» и двух минут ожидания. В ответе глагола они внутри
    # аллокации -- to_dict(task=True).
    task, reason = task_and_reason(alloc)
    slim = Alloc.from_dict({**alloc, "task": task, "reason": reason})
    meta = _api().node_meta(slim.node) or {}
    return {"ok": True, "alloc": slim.to_dict(task=True), "driver": meta.get("mop_driver"),
            **extra}


def _give_up(project, req):
    """Остановить джоб, чей bootstrap сдался (#345). Зовёт mop-bootstrap
    после ответа узлу; просителю не верим -- запись
    итога перечитывается, и стоп только если сдался текущий ключ. Стоп, а не
    снос (purge False): клон и тело на месте, `mop update` поднимает снова."""
    name = req["name"]
    if not bootstrap.gave_up_of(bootstrap.ROOT, name):
        return {"error": f"{name}: its bootstrap has not given up on the current workspace"}
    _api().deregister(name, purge=False)
    return {"ok": True, "name": name, "stopped": True}


STDERR_LINES, STDERR_MAX = 40, 500


def _stderr(project, req):
    """Хвост stderr задачи последней аллокации папета (#333): N строк.

    Нужен там, где пейна нет -- папет падает до tmux (bootstrap), и `mop
    tail` иначе не показал бы ничего; причина живёт только в stderr. Только
    чтение, поэтому без ворот владения, как alloc. Аллокации нет -- пусто,
    а не отказ: спрашивать некого."""
    asked = req.get("lines")
    n = min(max(int(STDERR_LINES if asked is None else asked), 1), STDERR_MAX)
    try:
        alloc = _api().latest_alloc(req["name"])
    except Exception:
        alloc = None
    if not alloc:
        return {"ok": True, "alloc": None, "status": None, "lines": []}
    a = Alloc.from_dict(alloc)
    text = _api().alloc_stderr(a.id, state.task_name(alloc))
    return {"ok": True, "alloc": a.id, "status": a.client_status,
            "lines": (text or "").splitlines()[-n:]}


def _spec(project, req):
    job = _api().get_job(req["name"])
    if not job:
        return {"error": f"no job {req['name']}"}
    return {"ok": True, "meta": domain.raw(job),
            "status": job.get("Status"), "stale": spec.spec_is_stale(job)}


def _roster(project, req):
    """Ростер глазами проекта: джоб плюс его аллокация. Отбор — тот же
    visible(): непомеченные видит только оператор, и это не два правила, а
    одно. Аллокация едет вместе с джобом — клиенту иначе пришлось бы идти за
    каждой отдельным запросом по сети.

    stale=True добавляет вердикт об устаревшей спеке, и только по просьбе: он
    стоит вызова API на каждый джоб, а нужен одному `mop server doctor`. Платить за
    него в каждом `mop list` было бы платой за чужой глагол."""
    return {"ok": True, "items": nomad_items(project, stale=bool(req.get("stale")))}


def _pool(project, req):
    """Ёмкость узлов пула. Проекту это положено: по свободным слотам мастер
    решает, заводить ли папета. Кто ещё живёт на узле — глагол `nodes`."""
    return {"ok": True, "nodes": nomad_pool()}


def _nodes(project, req):
    return {"ok": True, "nodes": nodes.nomad_rows(nomad_pool(), api=_api())}


def _drain(project, req):
    _api().node_drain(req["node"], int(req.get("deadline") or 300))
    return {"ok": True, "node": req["node"]}


def _up(project, req):
    _api().node_eligibility(req["node"], True)
    return {"ok": True, "node": req["node"]}


# Хосты инвентаря контроллера (#178): кладёт `mop server deploy` (роль cluster),
# инвентарь сам лежит у контроллера, куда учётке пула хода нет.
INVENTORY_HOSTS = paths.local("inventory-hosts.json")


def inventory_refusal(node, hosts):
    """Почему узел нельзя забыть по инвентарю; None -- можно (#178).

    Узел, оставшийся в инвентаре, следующий `mop server deploy` молча ставит снова,
    и forget оказывается не решением, а паузой до прогона. hosts=None --
    списка нет (сервер развёрнут до #178): не отказ, иначе оператор упёрся
    бы в файл, которого ему никто не клал."""
    if hosts is not None and node in hosts:
        return (f"{node} is still in the inventory — take it out and run "
                f"mop server deploy first, or the next deploy configures it again")
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
    summary = _api().node_summary(node)
    if summary is None:
        return {"error": f"no node {node} in the cluster"}
    why = nomad.forget_refusal(summary, _api().node_allocs(node))
    if why:
        return {"error": why}
    _api().node_forget(node)
    return {"ok": True, "node": node}


def _meta(project, req):
    _api().set_node_meta(req["node"], req.get("updates") or {})
    return {"ok": True, "node": req["node"], "meta": _api().node_meta(req["node"])}


def _gc_policy(project, req):
    """Единая политика удаления свободных папетов, заданная установкой."""
    return {"ok": True, "free_min_gb": config.num("MOP_GC_FREE_MIN_GB"),
            "max_per_run": config.num("MOP_GC_MAX_PER_RUN")}


def _client_config(req):
    """Только личный субъект после проверки NATS, не узловой server.rpc.

    Адрес -- публичный origin установки (#547, #557): llm.public_origin,
    ЕДИНСТВЕННАЯ формула https://<MOP_SERVER_LAN>:<MOP_HTTPS_PORT> -- inline
    копия здесь разошлась бы с дефолтом настройки и дефолтом окружения
    тел. Надстраивает /v1/* потребитель, и ровно один раз; nginx root
    /v1 уже отвечает на корне (#529)."""
    if not req.get("_caller"):
        return {"error": "client_config requires an authenticated personal subject"}
    key = proxykey.source({})
    if not key:
        return {"error": "proxy client key is not installed on the server"}
    return {"ok": True, "https_port": str(config.get("MOP_HTTPS_PORT")),
            "proxy_url": llm.public_origin(), "proxy_key": key}


# ─── глаголы: проекты (#117) ─────────────────────────────────────────────
# Реестр, лимиты и пароли папетов живут здесь, на сервере, и пишет их только
# сервис. Глаголы идут в потоках петли, поэтому правка реестра -- под замком:
# два одновременных завода иначе потеряли бы один из проектов.
#
# Реестр -- один атомарный снимок ~/.config/mop/projects.json (#534):
# {version:1, projects:[{origin, limit?}]}. Reader (глаголы-списки, лимит
# в add) читает, но НИКОГДА не пишет, мигрируя только в голове; миграцию
# на диск выполняет первый успешный ИЗМЕНЯЮЩИЙ глагол -- тот же замок, тот
# же один снимок: обрыв или падение между «реестр» и «лимиты» больше не
# разводит данные. Легаси-пара (projects + limits.json) остаётся на диске
# (её читает deploy до #540 и откат), но сервис её больше не пишет.
_projects_lock = threading.Lock()

# Ворота проекта (#567): add делает limit/count → name → workspace →
# register раздельно, а глаголы идут в потоках петли -- два одновременных
# add одного проекта оба читали пустой список джобов, оба выбирали
# pu-<проект>-1, и вторая регистрация перезаписывала спеку первой; другой
# interleating проводил обоих мимо последнего слота квоты. Ворота держат
# всю цепочку одним куском -- по имени проекта: независимые проекты не
# ждут друг друга. project_limit и project_delete берут те же ворота,
# чтобы add не проскочил между чтением лимита и регистрацией; порядок
# замков один -- реестровый снаружи, ворота внутри, инверсии нет.
# Граница гарантии -- один процесс cluster.
_project_gates = {}
_gates_lock = threading.Lock()


def _project_gate(name):
    """Ворота имени проекта: один threading.Lock на проект, навсегда."""
    with _gates_lock:
        got = _project_gates.get(name)
        if got is None:
            got = _project_gates[name] = threading.Lock()
        return got


def _snapshot():
    """Снимок реестра для writer'а: миграция требует представимости в v1,
    непредставимое -- отказ (#534). Каталог -- атрибут модуля на момент
    вызова: тесты и установки подменяют одно место."""
    return projects.snapshot_of(projects.SNAPSHOT_DIR)


def _view():
    """Снимок реестра для читателя (#534): старые данные видимы и без
    миграции -- отказ представимости блокирует мутацию, а не чтение.
    Читает, никогда не пишет."""
    return projects.view_of(projects.SNAPSHOT_DIR)


def _save(snapshot):
    """Запись снимка: единственный writer -- мутирующий глагол под замком."""
    projects.write_snapshot(snapshot, projects.SNAPSHOT)


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


def _with_rollback(snapshot, change):
    """Правка реестра с откатом: снимок и файл пользователей возвращаются,
    если шина нового пользователя не пустила. Снимок один (#534): реестр
    и лимиты возвращаются вместе, разводить их больше нечем.

    Формат переключает только УСПЕШНАЯ мутация (#534). Правда о том, была
    ли запись, -- сам диск, а не порядок исключений: откат смотрит байты
    снимка ДО и ПОСЛЕ провалившейся правки. Не менялись -- диск не трогаем
    (запись умерла до commit'а -- вслепую не перезаписываем). Файла не
    было, а теперь есть -- неуспешная мутация сняла создание. Были другие
    байты -- возвращаем прежние ЦЕЛИКОМ. Откат сам упал -- обе ошибки
    доезжают до ответа: проглоченный откат выглядел бы успехом с чужим
    состоянием на диске."""
    try:
        before = _snapshot_bytes()
    except OSError as e:
        raise RuntimeError(f"cannot read the registry to guard the change: {e}") from None
    users = natsconf.read_users()
    try:
        return change()
    except Exception as e:
        errors = [f"{e}"]
        try:
            now = _snapshot_bytes()
        except OSError as e2:
            now = _UNREADABLE
            errors.append(f"rollback failed: {e2}")
        if now != before:
            try:
                if before is None:
                    os.remove(projects.SNAPSHOT)
                else:
                    fsutil.write_atomic(projects.SNAPSHOT, before)
            except Exception as e2:  # noqa: BLE001 -- отказ отката доезжает до ответа
                errors.append(f"rollback failed: {e2}")
        if users is not None:
            # Компенсация users -- та же часть отката (#534): мы уже внутри
            # провалившейся правки, RPC всё равно отказан, и неудача этой
            # записи не «решать ли», а «не потерять ли» -- причина доезжает
            # к исходной. Секретное содержимое users в текст не попадает.
            try:
                natsconf.write(users)
            except Exception as e3:  # noqa: BLE001 -- отказ отката доезжает до ответа
                errors.append(f"users restore failed: {e3}")
            else:
                try:
                    natsconf.reload()
                except Exception as e3:  # noqa: BLE001 -- отказ отката доезжает до ответа
                    errors.append(f"reload failed: {e3}")
        if len(errors) > 1:
            # Исходный отказ + отказ отката: ложный успех хуже провала.
            raise RuntimeError("; ".join(errors)) from None
        raise


# Маркер «диск после провала не читается»: отличается от любого содержимого
# и от отсутствия, откат обязан попытаться вернуть прежние байты.
_UNREADABLE = object()


def _snapshot_bytes():
    """Байты projects.json либо None, если файла нет. Ошибка чтения --
    наружу: состояние реестра перед правкой обязано быть известно."""
    try:
        with open(projects.SNAPSHOT, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _projects(project, req):
    # Взгляд читателя (#534): проблемная легаси-пара не прячет старые
    # данные за отказом миграции -- refusing-миграция блокирует мутацию,
    # а список показывает то же, что показывал всегда.
    snap = _view()
    return {"ok": True, "names": projects.snapshot_names(snap),
            "origins": sorted(projects.snapshot_origins(snap)),
            "lines": sorted(projects.snapshot_lines(snap)),
            "limits": projects.snapshot_limits(snap)}


def _project_add(project, req):
    origin = req["origin"]
    name = puppets.project_of(origin)
    with _projects_lock:
        snap = _snapshot()
        new = projects.snapshot_with_origin(snap, origin)
        added = projects.snapshot_origins(new) != projects.snapshot_origins(snap)

        def change():
            _save(new)
            _users_apply(projects.snapshot_names(new), verify_user=name)
        _with_rollback(snap, change)
    return {"ok": True, "name": name, "added": added}


def _project_delete(project, req):
    name = req["name"]
    # Ворота проекта (#567) -- внутри реестрового замка, как у лимита:
    # снятие проекта и завод папета ему больше не расходятся.
    with _projects_lock, _project_gate(name):
        snap = _snapshot()
        new = projects.snapshot_without(snap, name)
        dropped = sorted(set(projects.snapshot_origins(snap))
                         - set(projects.snapshot_origins(new)))
        alive = [j["ID"] for j in nomad_jobs(bus.ADMIN)
                 if JobMeta.from_job(j).project == name]
        why = delete_refusal(name, dropped, alive)
        if why:
            return {"error": why}

        def change():
            _save(new)
            _users_apply(projects.snapshot_names(new))
        _with_rollback(snap, change)
        # Лимит снятого проекта (#107) уходит с ним -- он в том же снимке,
        # разводить их больше нечем. Пароль и секреты -- вне снимка, как
        # раньше: сервер раздавал бы кред пользователя, которого на шине
        # уже нет, а заведённый заново получил бы чужие секреты.
        try:
            os.remove(os.path.join(bootstrap.PUPPET_CREDS, creds.puppet_pass_file(name)))
        except FileNotFoundError:
            pass
        import shutil
        shutil.rmtree(project_secrets.project_dir(project_secrets.ROOT, name),
                      ignore_errors=True)
    return {"ok": True, "name": name, "dropped": dropped}


def _project_limit(project, req):
    name = req["name"]
    value = req.get("value")
    if value is not None:
        value = projects.parse_limit(str(value))
    # Ворота проекта (#567) -- внутри реестрового замка: add не проскочит
    # между чтением лимита и регистрацией; порядок замков один (реестровый
    # снаружи, ворота внутри), инверсии нет.
    with _projects_lock, _project_gate(name):
        snap = _snapshot()
        if name not in projects.snapshot_names(snap):
            return {"error": f"no project {name} in the registry; what the pool "
                             f"serves: mop project list"}
        _save(projects.snapshot_with_limit(snap, name, value))
    return {"ok": True, "name": name, "limit": value}


# ─── глаголы: реестр кредитов (#283) ─────────────────────────────────────
# Оператору: кредит -- авторизация у провайдера LLM на всю установку, а не
# на проект. Секрет едет по шине один раз, внутрь (cred_add), и наружу не
# возвращается ни одним глаголом: строки списка -- без него.
def _registered(project):
    """Отказ по незаведённому проекту: опечатка в имени завела бы секреты
    проекту, которого нет."""
    # Взгляд читателя (#534): секреты кладутся проекту из видимого реестра,
    # проблемная пара его не прячет -- отказ здесь не о миграции.
    if project not in projects.snapshot_names(_view()):
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
    # Держатель -- вызывающий (#207): логин из субъекта, на прежнем
    # субъекте -- названный телом.
    who = lease.caller(req)[0]
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
    "stderr":         Verb(_stderr,         PROJECT, True,  False),
    "spec":           Verb(_spec,           PROJECT, True,  True),
    "secret_put":     Verb(_secret_put,     SECRET,  False, False),
    "secret_list":    Verb(_secret_list,    SECRET,  False, False),
    "secret_remove":  Verb(_secret_remove,  SECRET,  False, False),
    "landing":        Verb(_landing,        PROJECT, False, False),
    # Итог прогона bootstrap'а (#334): читает, но называет джоба -- как spec.
    "bootstrap_result": Verb(_bootstrap_result, PROJECT, True, True),
    "nodes":          Verb(_nodes,          ADMIN,   False, False),
    "drain":          Verb(_drain,          ADMIN,   False, False),
    "up":             Verb(_up,             ADMIN,   False, False),
    "forget":         Verb(_forget,         ADMIN,   False, False),
    "meta":           Verb(_meta,           ADMIN,   False, False),
    "gc_policy":      Verb(_gc_policy,      ADMIN,   False, False),
    "projects":       Verb(_projects,       ADMIN,   False, False),
    "project_add":    Verb(_project_add,    ADMIN,   False, False),
    "project_delete": Verb(_project_delete, ADMIN,   False, False),
    "project_limit":  Verb(_project_limit,  ADMIN,   False, False),
    # Реестр кредитов (#283): на всю установку, оператору.
    # Аренда -- в тело своего папета (#360): мастеру проекта тоже, имя
    # проверяется по origin джоба, как у bootstrap_result. Нового права нет:
    # обработчик берёт из запроса одно имя, аренду -- из меты джоба, узел --
    # из Nomad, и отдаёт папету свежую копию его же аренды.
    # Сдавшийся bootstrap (#345): просит mop-bootstrap, стоп -- здесь.
    "give_up":         Verb(_give_up,         ADMIN,   False, False),
}
# Прежние наборы -- выводом из таблицы.
PROJECT_VERBS = tuple(v for v, d in VERBS.items() if d.scope in (PROJECT, SECRET))
SECRET_VERBS = tuple(v for v, d in VERBS.items() if d.scope == SECRET)
ADMIN_VERBS = tuple(v for v, d in VERBS.items() if d.scope == ADMIN)
NAMED_VERBS = tuple(v for v, d in VERBS.items() if d.named)
ACTING_VERBS = tuple(v for v, d in VERBS.items() if d.acting)


def answer(project, req, api=None):
    """Запрос от имени проекта -> ответ (dict). Синхронно: зовут в потоке.
    api -- nomad.NomadApi (#275); без него -- тот, что в контексте (живой).

    Владельца джоба узнаём ДО проверки и проверяем по нему, а не по имени:
    имя `pu-<проект>-<n>` собирается из того же basename, но правду о том,
    чей это папет, несёт только Meta.origin."""
    if api is not None:
        with using(api):
            return _answer(project, req)
    return _answer(project, req)


def _answer(project, req):
    verb = req.get("verb")
    if project is None:
        return _client_config(req) if verb == "client_config" else \
            {"error": f"no such join verb {verb}"}
    # Ограждение #603: принципал роли не говорит по легаси-rpc человека.
    # Его субъект -- role-канал (node.<n>.role.<grant>, cluster.role.<grant>),
    # и глагол резолвит сервер; легаси-ветку исполнял бы без резолвера
    # старый контроллер на смешанном роллауте. Отказ -- до таблиц глаголов.
    caller = req.get("_caller") or ""
    if soul.is_grant(caller):
        # Роль выключена -- протокол не рекламируется (полусобранный
        # сервер); сам шов флага не знает: грамматика -- не настройка.
        if not config.get("MOP_SOUL_ROLES"):
            return {"error": f"{caller}: grant principals speak on the role "
                             f"channel, not the legacy rpc (#603)"}
        # Авторизация -- шов реестра (#581 authorize_request), не своя
        # сборка: субъект собран busnames.cluster, глагол -- свежим
        # resolve, spawn-контекст -- тем же снимком. Отказ шва едет
        # дословно: его формулировка -- часть контракта реестра.
        ctx = grants.authorize_request(project, req, verb, root=SOUL_ROOT)
        if "error" in ctx:
            return ctx
        try:
            return _delegated(project, req, ctx)
        except ValueError as e:
            return {"error": f"add: {e}"}
        except Exception as e:
            # Причина едет с отказом: тихий «failed» убил бы диагностику
            # на полпути (#265).
            return {"error": f"add: {nomad.describe_error(e)}"}
    name = req.get("name")
    spec = _row(verb)
    origin, exists = (None, False)
    if spec and spec.named and name:
        origin, exists = _owner(name)
    # Смену репозитория update сервер считает сам (#365): origin запроса
    # против origin джоба. Поле new_origin присылает клиент, то есть сам
    # проверяемый, и границу проекта на нём держать нельзя.
    new_origin = None
    if verb == "update" and req.get("origin") and req.get("origin") != origin:
        new_origin = req.get("origin")
    # Владелец -- из джоба, если джоб есть (#365): origin запроса нужен
    # только add, где джоба нет. Иначе непомеченный джоб доставался бы
    # тому, кто подложил свой origin.
    owner = origin if exists else req.get("origin")
    why = refusal(project, verb, origin=owner, name=name,
                  job_exists=exists, new_origin=new_origin)
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
    what = (req.get("name") or req.get("node") or req.get("origin")
            or req.get("puppet") or "")
    # Кто просил (#207): логин из субъекта -- как есть; названный телом на
    # прежнем субъекте -- с пометкой, пока переход не убран.
    login, verified = lease.caller(req)
    by = "" if not login else f" by {login}" + ("" if verified else " (self-declared)")
    return [f"{project}.{req.get('verb')} {what}{by}: {out.get('error') or 'ok'}"]


def banner(subject, nomad_addr):
    return f"mop-cluster: subscribed to {subject}, Nomad at {nomad_addr}"


async def serve(log, api=nomad):
    """Подписчик сервера. Креды — оператора (admin): сервис слушает все
    проекты, а разделяет их проверкой проекта из субъекта. api -- Nomad
    (#275), по умолчанию живой."""
    # Оба субъекта (#207): с логином вызывающего и прежний, до уборки.
    subj = [busnames.cluster(busnames.ANY), busnames.cluster(busnames.ANY, login=busnames.ANY),
            busnames.join_config(busnames.ANY)]
    # Ключ LLM-прокси узлам (#391): клиентские пути раздачи умерли, владелец
    # ключа -- сервер. Старт -- не тик: ключ обязан опередить первый папет.
    proxykey.start(log)
    await service.serve("mop-cluster", subj, lambda project, req, _send: answer(project, req, api=api),
                        log, journal, lambda: banner(", ".join(subj), nomad.ADDR))
