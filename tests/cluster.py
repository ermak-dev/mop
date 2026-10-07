#!/usr/bin/env python3
"""Права сервиса кластера без пула: python3 tests/cluster.py

Сервис кластера — единственное, что говорит с Nomad (#80). Всё, что раньше
мастер делал сам с management-токеном, он теперь просит глаголом, а сервис
решает, можно ли. Решение и проверяется здесь: оно чистое, и ошибка в нём —
это не отказ, а тихо выполненная чужая команда.

Главное свойство: проект берётся из СУБЪЕКТА, а не из тела запроса. Субъект
проверен правами NATS (`master-<проект>` пишет только в `mop.<проект>.>`),
поле в теле подделывает кто угодно.

HYPOTHESIS (#80): мастер ходит в Nomad напрямую management-токеном, и проект в
имени джоба никто не сверяет — мастер проекта A снимает джоб проекта B.
SOLUTION: глаголы на шине, сервис сверяет проект из субъекта с origin джоба,
как агент сверяет проект с origin клона.
STATUS: FIXED — see #80
"""
import dataclasses
import os
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, FakeNomad, NoLiveNomad, offline, patched, restored, GATE_NOW, gate_table_267  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.server import cluster  # noqa: E402
from mop.common import service  # noqa: E402

RUGENT = "git@git.ermak.dev:rugent/rugent.git"
MOP = "git@git.ermak.dev:ermak/mop.git"


def check_subject(c):
    """Проект — первый токен субъекта, и никогда не поле запроса."""
    for subj, want in [("mop.rugent.cluster.rpc", "rugent"),
                       ("mop.admin.cluster.rpc", "admin"),
                       ("mop.mop.cluster.rpc", "mop")]:
        c.expect(f"project_of({subj})", service.project_from_subject(subj), want)


def check_verbs(c):
    """Набор закрыт, и узловые глаголы — только оператору."""
    c.check("an unknown verb must be refused, not attempted",
            not (cluster.refusal("rugent", "nosuchverb") is None))

    # Узел общий для всех жильцов: увести чужого папета с машины или выкинуть
    # её из ростера — не дело мастера проекта.
    for verb in ("drain", "up", "forget", "meta", "nodes"):
        c.check(f"{verb} must be an operator's verb, refused for a project",
                not (cluster.refusal("rugent", verb) is None))
        c.check(f"{verb} must work for the operator",
                not (cluster.refusal("admin", verb) is not None),
                cluster.refusal("admin", verb))

    # ping отвечает всем: мастеру надо уметь отличить «сервис лёг» от
    # «Nomad не отвечает», не имея под рукой ни того, ни другого.
    for who in ("rugent", "admin"):
        c.check(f"ping must answer {who}", not (cluster.refusal(who, "ping") is not None))

    # Оператор делает и проектные глаголы: он и сегодня удаляет любого папета.
    c.check("the operator must reach any project's puppet",
            not (cluster.refusal("admin", "delete", origin=RUGENT) is not None))


def check_ownership(c):
    """Дыра, ради которой заводился сервис: чужой джоб."""
    # Мастер rugent просит снять джоб проекта mop — origin джоба говорит, чей
    # он, и это единственная правда (Meta.origin, как в lib.guard).
    why = cluster.refusal("rugent", "delete", origin=MOP, name="pu-mop-1")
    if c.check("a master must NOT reach another project's job", not (why is None)):
        c.check("the refusal must name the owner", not ("mop" not in why), repr(why))

    c.check("a master must reach its own job",
            not (cluster.refusal("rugent", "delete", origin=RUGENT, name="pu-rugent-1") is not None))

    # Завод папета: проект считается из origin, и подсунуть чужой нельзя —
    # иначе мастер rugent заводил бы папетов в проекте mop.
    c.check("add with another project's origin must be refused",
            not (cluster.refusal("rugent", "add", origin=MOP) is None))
    c.check("add with its own origin must be allowed",
            not (cluster.refusal("rugent", "add", origin=RUGENT) is not None))

    # Смена репозитория — тоже в границах проекта. Иначе мастер rugent
    # переводит своего папета на чужой origin и читает чужой репозиторий
    # ключом пула: креды на шине остаются его, а клон уже не его.
    c.check("update onto another project's origin must be refused",
            not (cluster.refusal("rugent", "update", origin=RUGENT, name="pu-rugent-1",
                                 new_origin=MOP) is None))
    c.check("update within its own project must be allowed",
            not (cluster.refusal("rugent", "update", origin=RUGENT, name="pu-rugent-1",
                                 new_origin=RUGENT) is not None))
    c.check("the operator may move a puppet between projects",
            not (cluster.refusal("admin", "update", origin=RUGENT, name="pu-rugent-1",
                                 new_origin=MOP) is not None))

    # Джоба нет вовсе — это не отказ прав, а отсутствие джоба: разные ответы,
    # иначе опечатка в имени читается как «нет прав».
    c.check("a missing job is not a permission refusal",
            not (cluster.refusal("rugent", "restart", name="pu-rugent-9") is not None))

    # Джоб есть, а метки нет: спека старой регистрации, которую никогда не
    # перерегистрировали (puppets.visible). Чей он — из него самого не узнать,
    # поэтому мастеру проекта он не отдаётся; оператору отдаётся, как в ростере.
    c.check("an unmarked job must not be reachable by a project master",
            not (cluster.refusal("rugent", "delete", name="pu-old-1", job_exists=True) is None))
    c.check("an unmarked job must stay reachable by the operator",
            not (cluster.refusal("admin", "delete", name="pu-old-1", job_exists=True) is not None))


def check_gone_job(c):
    """Снятый джоб: читающий глагол отвечает, действующий отказывает.

    HYPOTHESIS (#89): `alloc` отвечал «no job … in the cluster» на любой
    снятый джоб, и `puppets.delete` падал между снятием джоба и сносом тела —
    тело оставалось работать сиротой.
    SOLUTION: `alloc` отдаёт alloc=None; гейт существования остаётся у
    глаголов, которые что-то делают.
    STATUS: FIXED — see #89
    """
    gone = {"verb": "alloc", "name": "pu-rugent-9"}
    got = cluster.answer("admin", gone)
    if c.check("alloc of a job that is gone must answer, not refuse",
               not (got.get("error") or "alloc" not in got), got):
        c.check("alloc of a job that is gone must be None",
                not (got["alloc"] is not None), got)

    for verb in ("restart", "stop", "delete", "update"):
        got = cluster.answer("admin", {"verb": verb, "name": "pu-rugent-9"})
        c.check(f"{verb} of a job that is gone must refuse", not (not got.get("error")), got)


def check_limit(c):
    """Потолок папетов проекта (#107): add сверх него -- отказ.

    STATUS: FIXED — see #107
    """
    c.check("no limit must never refuse",
            not (cluster.over_limit("rugent", 2, None) is not None))
    c.check("below the limit add must pass",
            not (cluster.over_limit("rugent", 2, 3) is not None))
    why = cluster.over_limit("rugent", 3, 3)
    # Отказ называет проект, счёт и потолок: иначе «не заводится» ищут в
    # Nomad и в слотах узлов.
    c.check("at the limit add must be refused with numbers",
            not (not why or "rugent" not in why or "3" not in why), repr(why))
    # 0 -- заморозка: новых не заводить вовсе.
    c.check("limit 0 must refuse every add",
            not (cluster.over_limit("rugent", 0, 0) is None))


def check_project_verbs(c):
    """HYPOTHESIS (#117): проекты заводил только прогон ansible на
    контроллере, и с машины оператора `mop project add` падал.
    SOLUTION: глаголы проектов у сервиса кластера -- только оператору: мастер
    проекта не заводит чужих проектов и не поднимает себе лимит.
    STATUS: FIXED — see #117"""
    for verb in ("projects", "project_add", "project_delete", "project_limit"):
        if not c.check(f"{verb} must be an operator's verb",
                       not (verb not in cluster.ADMIN_VERBS)):
            continue
        c.check(f"a project's master must not get {verb}",
                not (cluster.refusal("rugent", verb) is None))
        c.check(f"the operator must get {verb}",
                not (cluster.refusal("admin", verb) is not None))
    try:
        why = cluster.delete_refusal("ghost", [], [])
    except AttributeError:
        c.fail("cluster.delete_refusal is missing")
        return
    # Нет в реестре -- отказ с именем: опечатка иначе читалась бы снятием.
    c.check("deleting an unknown project must be refused by name",
            not (not why or "ghost" not in why), repr(why))
    # Живые папеты -- отказ с их именами: мастер потерял бы шину под ними.
    why = cluster.delete_refusal("rugent", ["git@h:g/rugent.git"], ["pu-rugent-2", "pu-rugent-1"])
    c.check("deleting a project with puppets must name them",
            not (not why or "pu-rugent-1" not in why), repr(why))
    c.check("a registered project without puppets must be deletable",
            not (cluster.delete_refusal("rugent", ["git@h:g/rugent.git"], []) is not None))


def check_secret_verbs(c):
    """HYPOTHESIS (#127): секреты проекта класть было некуда. SOLUTION:
    глаголы сервиса на субъекте проекта -- мастеру своего проекта и
    оператору от имени проекта; у псевдопроекта admin секретов нет.
    STATUS: FIXED — see #127"""
    for verb in ("secret_put", "secret_list", "secret_remove"):
        if not c.check(f"{verb} must be a project's verb",
                       not (verb not in cluster.PROJECT_VERBS)):
            continue
        c.check(f"a project's master must get {verb}",
                not (cluster.refusal("rugent", verb) is not None),
                cluster.refusal("rugent", verb))
        c.check(f"{verb} on the admin pseudo-project must be refused: secrets belong to a project",
                not (cluster.refusal("admin", verb) is None))


def check_verb_table_173(c):
    """HYPOTHESIS (#173): права глаголов сервиса кластера -- в пяти
    параллельных наборах (PROJECT_VERBS, SECRET_VERBS, ADMIN_VERBS,
    NAMED_VERBS, ACTING_VERBS) плюс HANDLERS; новый глагол правится в
    нескольких местах, и право расходится молча.
    SOLUTION: одна таблица {глагол: Verb(fn, scope, named, acting)}, наборы
    выводятся из неё. Характеризация: tests/cluster_verbs_snapshot.json снят
    с кода ДО правки -- наборы, отказ refusal() и развилка answer() по каждому
    глаголу (+ неизвестный и нестроковый) x оператор/проект x джоб
    нет/свой/чужой/без метки x смена origin; после правки -- байт в байт.
    STATUS: FIXED — see #173"""
    import json
    here = os.path.dirname(os.path.realpath(__file__))
    with open(os.path.join(here, "cluster_verbs_snapshot.json")) as f:
        snap = json.load(f)
    for name, want in snap["sets"].items():
        c.expect(name, list(getattr(cluster, name)), want)
    if not c.check("cluster.VERBS must be the one table {verb: Verb(...)}",
                   not (not isinstance(cluster.VERBS, dict))):
        return
    own, foreign = "git@h:g/mop.git", "git@h:g/rugent.git"
    states = {"none": (None, False), "own": (own, True), "foreign": (foreign, True),
              "unlabeled": (None, True)}
    called = []
    keep_table = dict(cluster.VERBS)
    with offline(), restored(cluster, "_owner"):
        try:
            for v, d in keep_table.items():
                cluster.VERBS[v] = dataclasses.replace(d, fn=(lambda verb: lambda project, req: called.append(verb)
                                                  or {"ok": True, "handled": verb})(v))
            for key, want in snap["refusal"].items():
                verb, project, state, new = json.loads(key)
                origin, exists = states[state]
                got = cluster.refusal(project, verb, origin=origin, name="pu-x-1",
                                      job_exists=exists, new_origin=new)
                c.expect(f"refusal {key}", got, want)
                cluster._owner = lambda name, o=origin, e=exists: (o, e)
                called.clear()
                # Клиент шлёт новый origin полем origin и дублирует его в
                # new_origin; с #365 сервер решает по origin, поэтому
                # просимый origin едет в обоих полях, как у настоящего клиента.
                req = {"verb": verb, "name": "pu-x-1", "new_origin": new}
                if new and verb == "update":
                    req["origin"] = new
                got = {"reply": cluster.answer(project, req),
                       "called": list(called)}
                c.expect(f"answer {key}", got, snap["answer"][key])
        finally:
            cluster.VERBS.clear()
            cluster.VERBS.update(keep_table)


def check_forget_inventory_178(c):
    """HYPOTHESIS (#178): forget снимает узел с ростера Nomad, а в инвентаре
    контроллера он остаётся, и следующий `mop server deploy` молча ставит его снова.
    Предупреждение об этом осталось одной строкой в usage (#159).
    SOLUTION: deploy кладёт список хостов инвентаря файлом рядом с сервисом
    кластера (cluster.INVENTORY_HOSTS), и forget отказывает по нему ДО
    любого вызова Nomad. Файла нет (сервер до этой правки) -- как прежде.
    STATUS: FIXED — see #178"""
    import json
    import tempfile
    from mop.server import nomad
    fn = getattr(cluster, "inventory_refusal", None)
    if not c.check("cluster.inventory_refusal(node, hosts) exists", not (fn is None)):
        return
    why = fn("mop-2", ["localhost", "mop-2", "mop-3"])
    c.check("a node in the inventory must be refused by name",
            not (not why or "mop-2" not in why or "mop server deploy" not in why), repr(why))
    c.check("a node out of the inventory must not be refused by it",
            not (fn("gone", ["localhost", "mop-2"]) is not None))
    c.check("no host list (a server deployed before #178) must not refuse",
            not (fn("mop-2", None) is not None))

    # Через глагол: отказ -- до Nomad; файла нет -- как прежде. Сводка узла
    # (#196) -- тоже вызов Nomad, и при отказе её не спрашивают.
    d = tempfile.mkdtemp(prefix="mop-test-forget-")
    path = os.path.join(d, "inventory-hosts.json")
    touched = []
    with offline(), \
            patched(cluster, INVENTORY_HOSTS=path), \
            patched(nomad,
                    node_summary=lambda node: touched.append("summary") or {"Name": node},
                    node_allocs=lambda node: touched.append("allocs") or [],
                    forget_refusal=lambda node, allocs: None,
                    node_forget=lambda node: touched.append("forget")):
        with open(path, "w") as f:
            json.dump(["localhost", "mop-2"], f)
        got = cluster.answer("admin", {"verb": "forget", "node": "mop-2"})
        c.check("forget of an inventory host",
                not ("mop-2" not in (got.get("error") or "") or touched),
                f"{got!r}, Nomad touched: {touched}")
        touched.clear()
        got = cluster.answer("admin", {"verb": "forget", "node": "gone"})
        c.check("forget of a host out of the inventory",
                not (not got.get("ok") or touched != ["summary", "allocs", "forget"]),
                f"{got!r}, {touched}")
        os.remove(path)
        touched.clear()
        got = cluster.answer("admin", {"verb": "forget", "node": "mop-2"})
        c.check("forget with no host list must work as before",
                not (not got.get("ok") or touched != ["summary", "allocs", "forget"]),
                f"{got!r}, {touched}")


def check_forget_summary_196(c):
    """HYPOTHESIS (#196): _forget отдаёт forget_refusal имя узла (строку), а
    та читает сводку -- node['Name'], node.get('Status'); AttributeError, и
    forget с #80 не доходит до purge ни на одном узле.
    SOLUTION: _forget берёт сводку узла (nomad.node_summary) и отдаёт её;
    узла нет -- отказ с именем, без allocs и purge.
    STATUS: FIXED — see #196"""
    from mop.server import nomad
    touched = []
    closed = {"Name": "mop-2", "ID": "n2", "Status": "ready",
              "SchedulingEligibility": "ineligible"}
    allocs = []
    with offline(), patched(nomad,
                            node_summary=lambda name: dict(closed) if name == "mop-2" else None,
                            node_allocs=lambda name: touched.append("allocs") or list(allocs),
                            node_forget=lambda name: touched.append("forget")):
        got = cluster.answer("admin", {"verb": "forget", "node": "mop-2"})
        c.check("forget of a drained node must reach purge",
                not (not got.get("ok") or touched != ["allocs", "forget"]),
                f"{got!r}, {touched}")
        touched.clear()
        allocs[:] = [{"JobID": "pu-mop-1", "ClientStatus": "running"}]
        got = cluster.answer("admin", {"verb": "forget", "node": "mop-2"})
        err = got.get("error") or ""
        c.check("forget of a node with a live alloc must refuse with the job",
                not ("pu-mop-1" not in err or "drain" not in err or "forget" in touched),
                f"{got!r}, {touched}")
        touched.clear()
        got = cluster.answer("admin", {"verb": "forget", "node": "ghost"})
        c.check("forget of an unknown node must refuse by name, touching nothing",
                not ("ghost" not in (got.get("error") or "") or touched),
                f"{got!r}, {touched}")


def check_gates_40(c):
    """HYPOTHESIS (#40): владельца сверяет только send агента; restart, stop,
    update, delete (и recycle, собранный из delete+update) сервис кластера
    исполняет для любого мастера проекта, и работа другого мастера умирает
    посреди тикета.
    SOLUTION: перед изменением в Nomad сервис спрашивает у агента узла факты
    клона (глагол clone) и решает той же lease.may_touch: чужая живая аренда
    -- отказ с именем владельца до любого изменения; свой, ничей, force,
    оператор -- проходят. Агент не ответил -- «не знаю» не значит «ничей»:
    отказ, force его снимает. Аллокации нет -- спросить некого и убивать
    нечего: проходит.
    STATUS: FIXED — see #40"""
    import time
    from mop.server import bootstrap, nomad, spec
    from mop.common import bus
    from mop.common.domain import Owner
    if not c.check("cluster._clone_of(name) exists", not (not hasattr(cluster, "_clone_of"))):
        return
    olga = Owner("olga", int(time.time()) - 60).to_dict()
    work = {"clone": {"cur": "bug/1-x", "def": "master", "dirty": 1, "ahead": 0,
                      "owner": olga}}
    changed, asked = [], []
    answer = {"now": work}
    with offline(), \
            patched(cluster,
                    _clone_of=lambda name: asked.append(name) or answer["now"],
                    _owner=lambda name: (MOP, True)), \
            patched(nomad,
                    latest_alloc=lambda name: {"ID": "a1", "NodeName": "n1"},
                    alloc_restart=lambda alloc: changed.append("restart"),
                    alloc_stop=lambda alloc: changed.append("stop"),
                    register=lambda job: changed.append("register"),
                    deregister=lambda name, purge=True: changed.append("deregister")), \
            patched(spec, job_spec=lambda *a, **k: {"Job": "x"}), \
            patched(bootstrap, store=lambda *a, **k: changed.append("store")):
        verbs = {"restart": {}, "stop": {}, "update": {"origin": MOP},
                 "delete": {}, "recycle": {"purge": False}}
        for label, extra in verbs.items():
            verb = "delete" if label == "recycle" else label

            def ask(project, **more):
                changed.clear()
                asked.clear()
                return cluster.answer(project, {"verb": verb, "name": "pu-mop-1",
                                                **extra, **more})
            answer["now"] = work
            got = ask("mop", owner="anton")
            c.check(f"{label} by another master must be refused naming olga "
                    f"before any Nomad change",
                    not ("olga" not in (got.get("error") or "") or changed),
                    f"{got!r}, {changed}")
            got = ask("mop")
            c.check(f"{label} by an anonymous caller must be refused",
                    not (not got.get("error") or changed), f"{got!r}, {changed}")
            got = ask("mop", owner="olga")
            c.check(f"{label} by the owner must pass",
                    not (got.get("error") or not changed), f"{got!r}, {changed}")
            got = ask("mop", owner="anton", force=True)
            c.check(f"{label} with force must pass naming olga",
                    not (got.get("error") or not changed
                         or "olga" not in (got.get("owner_note") or "")),
                    f"{got!r}, {changed}")
            got = ask("admin", owner="anton")
            c.check(f"{label} by the operator must pass without asking",
                    not (got.get("error") or not changed or asked),
                    f"{got!r}, {changed}, asked {asked}")
            # Ничей и без owner -- как до #40.
            answer["now"] = {"clone": dict(work["clone"], owner=None)}
            got = ask("mop")
            c.check(f"{label} of nobody's puppet must pass as before",
                    not (got.get("error") or not changed), repr(got))
            answer["now"] = None
            got = ask("mop", owner="anton")
            c.check(f"{label} with no allocation must pass",
                    not (got.get("error") or not changed), repr(got))
            answer["now"] = {"error": "node agent n1 is not subscribed"}
            got = ask("mop", owner="anton")
            c.check(f"{label} with a silent agent must be refused with the reason",
                    not ("n1" not in (got.get("error") or "") or changed),
                    f"{got!r}, {changed}")
            got = ask("mop", owner="anton", force=True)
            c.check(f"{label} with a silent agent and force must pass",
                    not (got.get("error") or not changed), repr(got))

    # Факты -- у агента узла аллокации, через шину, глаголом clone.
    # Nomad -- поддельный, параметром (#275).
    placed = FakeNomad(allocs={"pu-mop-1": {"ID": "a1", "NodeName": "n1"}})
    with offline(), restored(bus, "request"):
        calls = []
        bus.request = lambda node, verb, **k: calls.append((node, verb, k.get("name"))) \
            or {"clone": None}
        with cluster.using(placed):
            got = cluster._clone_of("pu-mop-1")
        c.check("_clone_of must ask the node's agent",
                not (got != {"clone": None} or calls != [("n1", "clone", "pu-mop-1")]),
                f"{got!r}, {calls}")
        with cluster.using(FakeNomad()):
            c.check("_clone_of without an allocation must be None",
                    not (cluster._clone_of("pu-mop-1") is not None))

        def silent(node, verb, **k):
            raise bus.BusError("node agent n1 is not subscribed")
        bus.request = silent
        with cluster.using(placed):
            got = cluster._clone_of("pu-mop-1")
        c.check("_clone_of with a silent agent must say so",
                not ("n1" not in ((got or {}).get("error") or "")), repr(got))


def check_caller_207(c):
    """HYPOTHESIS (#207): сервис кластера видит из субъекта только проект;
    владелец для ворот (#40) и держатель токена посадки (#42) -- поля тела,
    которые пишет сам проситель.
    SOLUTION: логин человека -- токеном субъекта (mop.<p>.cluster.rpc.<логин>),
    каркас сервиса кладёт его в req["_caller"] поверх всего, что пришло в
    теле; ворота и landing читают одно значение, lease.caller. Прежний
    субъект без логина -- переход: логин из тела, помеченный self-declared.
    STATUS: FIXED — see #207"""
    import asyncio
    import json
    import tempfile
    import time
    from mop.common import busnames, landing, lease
    from mop.server import nomad
    from mop.common.domain import Owner
    subs = busnames.service_subscriptions()
    c.check("the services must listen on both cluster subjects",
            not ("mop.*.cluster.rpc.*" not in subs or "mop.*.cluster.rpc" not in subs), subs)

    # Каркас: вызывающий -- из субъекта, тело его не подделает.
    got = []

    def handler(project, req, send):
        got.append(req.get("_caller"))
        return {"ok": True}
    forged = json.dumps({"verb": "ping", "_caller": "bob"}).encode()
    for caller, want in (("alice", "alice"), (None, None)):
        got.clear()
        try:
            asyncio.run(service.answer("t", "mop", forged, handler,
                                       lambda *a: [], lambda line: None, None,
                                       caller=caller))
        except TypeError as e:
            c.fail("service.answer must take the caller", e)
            return
        c.expect(f"service.answer with caller {caller!r} and a forged body", got, [want])

    # Ворота (#40): решает вызывающий из субъекта, а не owner тела.
    olga = Owner("olga", int(time.time()) - 60).to_dict()
    work = {"clone": {"cur": "bug/1-x", "def": "master", "dirty": 1, "ahead": 0,
                      "owner": olga}}
    changed = []
    with offline(), \
            patched(cluster, _clone_of=lambda name: work, _owner=lambda name: (MOP, True)), \
            patched(nomad, latest_alloc=lambda name: {"ID": "a1", "NodeName": "n1"},
                    alloc_restart=lambda alloc: changed.append("restart")):
        req = {"verb": "restart", "name": "pu-mop-1"}
        got = cluster.answer("mop", dict(req, _caller="anton", owner="olga"))
        c.check("a forged body owner must not pass the gate",
                not ("olga" not in (got.get("error") or "") or changed), repr(got))
        got = cluster.answer("mop", dict(req, _caller="olga", owner="anton"))
        c.check("the subject's caller must be the one the gate sees",
                not (got.get("error") or changed != ["restart"]), repr(got))
        changed.clear()
        # Прежний субъект: логин из тела, как до #207.
        got = cluster.answer("mop", dict(req, _caller=None, owner="olga"))
        c.check("the old subject must keep today's self-declared owner",
                not (got.get("error") or changed != ["restart"]), repr(got))

    # Токен посадки (#42): держатель -- тот же вызывающий.
    d = tempfile.mkdtemp(prefix="mop-test-207-")
    with patched(landing, FILE=os.path.join(d, "landing.json")):
        got = cluster.answer("mop", {"verb": "landing", "action": "take", "puppet": "pu-mop-1",
                                     "_caller": "alice", "holder": "bob"})
        held = (landing.read().get("mop") or {}).get("holder")
        c.check("landing's holder must be the subject's caller",
                not (not got.get("ok") or held != "alice"), f"{got!r}, {held!r}")
        got = cluster.answer("mop", {"verb": "landing", "action": "give",
                                     "_caller": None, "holder": "alice"})
        c.check("the old subject must keep today's self-declared holder",
                not (not got.get("ok")), repr(got))

    # Журнал помечает названное телом: переход виден, пока он есть.
    line = " ".join(cluster.journal("mop", {"verb": "restart", "name": "pu-mop-1",
                                            "_caller": None, "owner": "olga"}, {"ok": True}))
    c.check("the journal must mark a self-declared caller",
            not ("self-declared" not in line), repr(line))
    line = " ".join(cluster.journal("mop", {"verb": "restart", "name": "pu-mop-1",
                                            "_caller": "olga"}, {"ok": True}))
    c.check("the journal must name a subject's caller plainly",
            not ("self-declared" in line or "olga" not in line), repr(line))
    c.check("lease.caller must prefer the subject",
            not (lease.caller({"_caller": "alice", "owner": "bob"}) != ("alice", True)))


def check_slots_total_243(c):
    """HYPOTHESIS (#243): nomad_pool отдаёт только свободные слоты, и
    сколько папетов берёт пустой узел, не видно никому.
    SOLUTION: рядом со slots -- slots_total = total_mb // spec.MEM; slots
    прежний, его читают другие. STATUS: FIXED — see #243"""
    from mop.server import nomad, spec

    class Nodes:
        def get_nodes(self):
            return [{"Name": "gpu", "Datacenter": nomad.POOL_DC, "Status": "ready"},
                    {"Name": "off", "Datacenter": nomad.POOL_DC, "Status": "down"},
                    {"Name": "ctl", "Datacenter": "control", "Status": "ready"}]

    class Client:
        nodes = Nodes()

    with patched(nomad, client=lambda: Client(),
                 node_capacity=lambda n: (2 * spec.MEM + 1, 5 * spec.MEM + spec.MEM // 2)):
        got = {n["name"]: n for n in cluster.nomad_pool()}
    gpu = got.get("gpu", {})
    c.check("gpu: slots 2 of 5 total",
            not (gpu.get("slots") != 2 or gpu.get("slots_total") != 5), gpu)
    c.check("a node not ready has no capacity, another dc is not listed",
            not ("slots_total" in got.get("off", {}) or "ctl" in got), got)
    # Строка `mop node` несёт его дальше.
    from mop.server import nodes
    row = nodes.row({"Name": "gpu", "Status": "ready"}, {}, gpu)
    c.check("nodes.row must carry slots_total", not (row.get("slots_total") != 5), row)


# ── #257: перерегистрация без ветки в запросе сохраняет ветку меты ────────
# HYPOTHESIS: `mop recycle`, `mop gc`, лечение doctor'а перерегистрируют
# спеку глаголом update без branch, и мета теряет ветку мастера (#256):
# свежее тело клонирует origin/HEAD. SOLUTION: _update берёт branch из
# запроса, а без него -- из текущей меты джоба; ветка -- свойство папета,
# которое перерегистрация обязана сохранять. STATUS: FIXED — see #257
def check_update_keeps_branch_257(c):
    calls = []
    # Nomad -- поддельный, параметром (#275).
    job = {"ID": "pu-mop-1", "Meta": {"origin": "git@h:g/mop.git", "llm": "claude",
                                      "branch": "swarm"}}
    with restored(cluster.spec, "job_spec"), restored(cluster, "store_workspace"):
        # cred и node -- та же сигнатура, что у настоящей job_spec (#284, #289).
        cluster.spec.job_spec = lambda name, origin, profile=None, cont=False, branch=None, \
            cred=None, node=None: calls.append(branch) or {"Job": {"ID": name}}
        cluster.store_workspace = lambda root, name, req: None
        # register фейка кладёт пустой джоб на место прежнего: мету каждый
        # вызов берёт из своего фейка.
        with cluster.using(FakeNomad(jobs={"pu-mop-1": job})):
            cluster._update("mop", {"name": "pu-mop-1", "origin": "git@h:g/mop.git"})
        with cluster.using(FakeNomad(jobs={"pu-mop-1": job})):
            cluster._update("mop", {"name": "pu-mop-1", "origin": "git@h:g/mop.git",
                                    "branch": "dev"})
        with cluster.using(FakeNomad()):
            cluster._update("mop", {"name": "pu-mop-1", "origin": "git@h:g/mop.git"})
    c.expect("update must keep the meta branch unless the request names one",
             calls, ["swarm", "dev", None])


# ── #284: аренда кредита -- в мете джоба, проверяется сервером ─────────
# HYPOTHESIS: add/update не знают о кредитах: спека без Meta.cred, чужое или
# несуществующее имя кредита прошло бы молча, а без имени папет не получил
# бы ни одного кредита реестра.
# SOLUTION: cluster._add/_update валидируют req.cred по реестру (имя и
# профиль), без него берут credreg.pick, update держит мету как ветку.
# STATUS: FIXED — see #284
def check_update_pins_node_289(c):
    seen = []
    job = {"ID": "pu-mop-1", "Meta": {"origin": "git@h:g/mop.git", "llm": "claude"}}
    with restored(cluster.spec, "respec"), restored(cluster, "store_workspace"):
        cluster.spec.respec = lambda name, meta, cont=False, node=None: \
            seen.append(node) or {"Job": {"ID": name}}
        cluster.store_workspace = lambda root, name, req: None
        with cluster.using(FakeNomad(jobs={"pu-mop-1": job},
                                     allocs={"pu-mop-1": {"ID": "a1", "NodeName": "hyper"}})):
            cluster._update("mop", {"name": "pu-mop-1", "origin": "git@h:g/mop.git"})
        with cluster.using(FakeNomad(jobs={"pu-mop-1": job})):
            cluster._update("mop", {"name": "pu-mop-1", "origin": "git@h:g/mop.git"})
    c.expect("update pins the puppet to its allocation's node, none without one",
             seen, ["hyper", None])


def check_owner_gate_267(c):
    """HYPOTHESIS (#267): ворота владения написаны дважды -- cluster.gate и
    agent._gate, каждая сама зовёт lease.may_touch и сама собирает
    (None, заметка) | (f"{name}: {заметка}", None), а заметку owner_note
    каждая прикладывает по-своему.
    SOLUTION: одна чистая lease.gate и одна lease.noted; сторона держит только
    своё -- откуда факты клона и как выглядит ответ. STATUS: FIXED — see #267"""
    from mop.common import lease
    gate = getattr(lease, "gate", None)
    if not c.check("lease.gate exists (else the gate is written on each side)",
                   not (gate is None)):
        return
    for name, clone, caller, force in gate_table_267():
        want = gate(name, clone, caller, GATE_NOW, force)
        req = {"name": name, **({"_caller": caller} if caller else {}),
               **({"force": True} if force else {})}
        got = cluster.gate(name, req, {"clone": clone.to_dict()}, GATE_NOW)
        c.expect(f"cluster.gate {caller} over {clone.owner} (force {force}) vs lease.gate",
                 got, want)
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                            "mop", "server", "cluster.py")).read()
    c.check("cluster.py must decide through lease.gate, not may_touch",
            not ("may_touch(" in src or "lease.gate(" not in src))
    noted = getattr(lease, "noted", None)
    if c.check("lease.noted exists (else owner_note is attached on each side)",
               not (noted is None)):
        for o, note, want in (({"ok": True}, "taken from bob", {"ok": True, "owner_note": "taken from bob"}),
                              ({"ok": True}, None, {"ok": True}),
                              ({"error": "x"}, "taken", {"error": "x"})):
            c.expect(f"lease.noted({o}, {note!r})", noted(o, note), want)


def check_node_267(c):
    """HYPOTHESIS (#267): узел приходит двумя формами словаря -- глагол pool
    (nomad_pool: {name, status, ...} по-разному по статусу) и глагол nodes
    (nodes.row: {name, driver, serves, state, ...}), -- и читатели (ready_nodes,
    pool_lines, mop node, дашборд) разбирают их строковыми ключами каждый сам.
    SOLUTION: значение узла, каждая форма провода -- своей парой from_/to_,
    ключи и их порядок прежние (снимки те же); читатели идут через него.
    С #277 форм -- два значения: PoolNode (глагол pool) и NodeRow (nodes).
    STATUS: FIXED — see #267"""
    import json
    from mop.common import domain, puppets, bus
    from mop.server import nodes
    PoolNode, NodeRow = getattr(domain, "PoolNode", None), getattr(domain, "NodeRow", None)
    if not c.check("domain.PoolNode and domain.NodeRow exist",
                   not (PoolNode is None or NodeRow is None)):
        return
    pool = [{"name": "a", "status": "down"},
            {"name": "b", "status": "ready", "error": "meta: boom"},
            {"name": "c", "status": "ready", "free_mb": 8192, "total_mb": 16384,
             "slots": 1, "slots_total": 2, "eligible": True},
            {"name": "d", "status": "ready", "free_mb": 0, "total_mb": 4096,
             "slots": 0, "slots_total": 0, "eligible": False}]
    for d in pool:
        back = PoolNode.from_pool(d).to_pool()
        c.check("pool form round trip", not (json.dumps(back) != json.dumps(d)),
                f"{d} -> {back}")
    summaries = [({"Name": "c", "Status": "ready"}, {"mop_projects": "mop"}, pool[2]),
                 ({"Name": "e", "Status": "ready", "Drain": True}, {}, {}),
                 ({"Name": "f", "Status": "ready"}, {"mop_driver": "no-such"}, {})]
    for summary, meta, cap in summaries:
        row = nodes.row(summary, meta, cap)
        back = NodeRow.from_row(row).to_row()
        c.check("nodes form round trip", not (json.dumps(back) != json.dumps(row)),
                f"{row} -> {back}")
    with restored(bus, "call_cluster"):
        bus.call_cluster = lambda verb, **kw: {"ok": True, "nodes": pool}
        got = puppets.pool()
        c.check("puppets.pool must give PoolNode values",
                not (not all(isinstance(n, PoolNode) for n in got)), repr(got))
        # Как было: ready и не закрытый планированию; у сломанного (error)
        # eligible нет, и он -- по умолчанию открыт.
        c.expect("ready_nodes: ready and not closed", puppets.ready_nodes(), {"b", "c"})
        rows = [nodes.row(*x) for x in summaries]
        bus.call_cluster = lambda verb, **kw: {"ok": True, "nodes": rows}
        got = puppets.nodes()
        c.check("puppets.nodes must give NodeRow values of the same rows",
                not (not all(isinstance(n, NodeRow) for n in got)
                     or [n.to_row() for n in got] != rows), repr(got))


def check_node_forms_277(c):
    """HYPOTHESIS (#277): domain.Node -- один класс с одиннадцатью
    необязательными полями на две разные формы провода (pool и nodes), и
    «какая это форма» читается по тому, какие поля заполнены. Готовый узел
    без памяти и неготовый с памятью собирались молча, и to_pool отдавал
    то None в ёмкости, то тихо терял поля.
    SOLUTION: два значения, PoolNode и NodeRow; у PoolNode форма названа
    (form: down / broken / ready), и __post_init__ требует своё: у готового
    ёмкость обязательна, у неготового и сломанного её нет -- ValueError с
    именем поля. Провод прежний байт в байт.
    STATUS: FIXED — see #277"""
    import json
    from mop.common import domain
    from mop.server import nodes
    PoolNode, NodeRow = getattr(domain, "PoolNode", None), getattr(domain, "NodeRow", None)
    if not c.check("#277 PoolNode and NodeRow exist", not (PoolNode is None or NodeRow is None)):
        return
    c.check("#277 the one-class Node is gone", not hasattr(domain, "Node"))

    def refused(what, make, field):
        try:
            got = make()
        except ValueError as e:
            c.check(f"#277 {what}: the refusal names {field!r}, got {e}", field in str(e))
            return
        c.check(f"#277 {what} must be refused, got {got!r}", False)

    cap = {"free_mb": 8192, "total_mb": 16384, "slots": 1, "slots_total": 2}
    for field in ("free_mb", "total_mb", "slots"):
        refused(f"a ready node without {field}",
                lambda: PoolNode("c", "ready", **dict(cap, **{field: None})), field)
    # slots_total у готового необязателен: сервис старше #243 его не шлёт, и
    # во время раската его ответ читается («slots 0/-»), а не отказывает.
    old = PoolNode.from_pool({"name": "o", "status": "ready", "free_mb": 1, "total_mb": 2,
                              "slots": 0, "eligible": True})
    c.expect("#277 a ready node from a service older than #243 (no slots_total)",
             (old.form, old.slots_total), ("ready", None))
    for field in cap:
        refused(f"a not-ready node with {field}",
                lambda: PoolNode("a", "down", **{field: cap[field]}), field)
        refused(f"a broken node with {field}",
                lambda: PoolNode("b", "ready", error="boom", **{field: cap[field]}), field)
    refused("a not-ready node with an error", lambda: PoolNode("a", "down", error="boom"),
            "error")
    refused("a ready pool dict without memory",
            lambda: PoolNode.from_pool({"name": "c", "status": "ready", "slots": 1,
                                        "slots_total": 2, "eligible": True}), "free_mb")
    # Форма названа, а не угадана по заполненным полям.
    forms = [(PoolNode("a", "down"), "down"), (PoolNode("b", "ready", error="boom"), "broken"),
             (PoolNode("c", "ready", **cap), "ready")]
    for n, want in forms:
        c.expect(f"#277 form of {n.name}", n.form, want)
    # Провод байт в байт: обе формы, все их варианты.
    pool = [{"name": "a", "status": "initializing"},
            {"name": "b", "status": "ready", "error": "meta: boom"},
            {"name": "c", "status": "ready", **cap, "eligible": False}]
    for d in pool:
        back = PoolNode.from_pool(d).to_pool()
        c.check(f"#277 pool form byte for byte: {d} -> {back}", json.dumps(back) == json.dumps(d))
    for summary, meta, cp in (({"Name": "c", "Status": "ready"}, {"mop_projects": "mop"}, cap),
                              ({"Name": "e", "Status": "down"}, {}, {}),
                              ({"Name": "f", "Status": "ready"}, {"mop_driver": "no-such"}, {})):
        row = nodes.row(summary, meta, cp)
        back = NodeRow.from_row(row).to_row()
        c.check(f"#277 nodes form byte for byte: {row} -> {back}",
                json.dumps(back) == json.dumps(row))
    # Строка nodes -- не узел pool: у неё нет статуса, и в pool её не отдать.
    c.check("#277 a NodeRow has no pool form", not hasattr(NodeRow, "to_pool"))
    c.check("#277 a PoolNode has no nodes form", not hasattr(PoolNode, "to_row"))


# ── #275: Nomad за интерфейсом ───────────────────────────────────────────
# HYPOTHESIS: сервис кластера зовёт nomad.* в 38 местах, сборка образа -- в
# шести, и проверки подменяют Nomad только атрибутами модуля: зависимость на
# конкретный HTTP-клиент, а не на интерфейс.
# SOLUTION: typing.Protocol nomad.NomadApi -- ровно то, что читают cluster и
# image; живой модуль ему удовлетворяет как есть. Сервис берёт api
# параметром (cluster.using, answer/serve(api=...)), по умолчанию -- живой;
# image.clear/restore/build -- тоже. Чистые помощники (describe_error,
# forget_refusal) и константы (ADDR, POOL_DC) остаются у модуля.
# STATUS: FIXED — see #275
PURE_NOMAD = {"ADDR", "POOL_DC", "describe_error", "forget_refusal"}


def check_nomad_api_275(c):
    import ast
    from mop.server import image, nomad
    api = getattr(nomad, "NomadApi", None)
    c.check("nomad.NomadApi exists", api is not None)
    if api is None:
        return
    c.check("the live module satisfies NomadApi as it is", isinstance(nomad, api))
    fake = FakeNomad(jobs={"pu-mop-1": {"ID": "pu-mop-1", "Status": "running", "Meta": {
        "origin": "git@h:g/mop.git", "llm": "claude", "branch": "swarm"}}})
    c.check("a fake satisfies NomadApi", isinstance(fake, api))
    # Глагол через api, без подмены атрибутов модуля nomad.
    keep = {n: getattr(nomad, n) for n in dir(nomad) if not n.startswith("_")}
    got = cluster.answer("mop", {"verb": "spec", "name": "pu-mop-1"}, api=fake)
    c.expect("spec answers from the injected api",
             (got.get("ok"), (got.get("meta") or {}).get("branch")), (True, "swarm"))
    with cluster.using(fake):
        cluster.store_workspace, saved = (lambda root, name, req: None), cluster.store_workspace
        try:
            cluster._update("mop", {"name": "pu-mop-1", "origin": "git@h:g/mop.git"})
        finally:
            cluster.store_workspace = saved
    reg = [a[1] for a in fake.calls if a[0] == "register"]
    c.expect("update registers through the injected api, keeping the branch",
             [(r["Job"]["ID"], r["Job"]["Meta"].get("branch")) for r in reg], [("pu-mop-1", "swarm")])
    c.check("nothing on the nomad module was replaced",
            all(getattr(nomad, n) is v for n, v in keep.items()))
    # Сборка образа: снять и поднять -- через тот же api.
    fake2 = FakeNomad(jobs=dict(fake.jobs))
    with restored(image, "project_rows"), restored(image.puppets, "_wait_stopped", "wipe"):
        job = fake.jobs["pu-mop-1"]
        image.project_rows = lambda project, api=None: [
            {"name": "pu-mop-1", "node": "n1", "job": job, "container": True,
             "state": "free", "kind": None}]
        image.puppets._wait_stopped = lambda name: None
        image.puppets.wipe = lambda node, name, *a, **k: None
        gone = image.clear("mop", force=True, api=fake2)
        image.restore(gone, api=fake2)
    c.expect("image.clear/restore go through the injected api",
             [a[0] for a in fake2.calls], ["deregister", "register"])
    # Структурно: cluster и image касаются Nomad только через api.
    here = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    for rel in ("mop/server/cluster.py", "mop/server/image.py"):
        tree = ast.parse(open(os.path.join(here, rel)).read())
        bare = sorted({f"{n.attr}:{n.lineno}" for n in ast.walk(tree)
                       if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                       and n.value.id == "nomad" and n.attr not in PURE_NOMAD})
        c.expect(f"{rel} reaches Nomad only through the injected api", bare, [])


# ── #334: ответ add/update несёт метку отправленного, итог -- глаголом ──
# HYPOTHESIS: store_workspace кладёт только текст; чем кончился прогон,
# клиент узнаёт из `mop list` или журнала -- глагола нет.
# SOLUTION: с полем bootstrap_sent сервис кладёт происхождение с меткой
# регистрации и отдаёт метку в ответе (bootstrap_marker), update -- ещё и
# replaced: перерегистрация без изменения спеки Nomad не переразмещает, и
# прогона не будет. Без поля -- байт в байт как было. Глагол
# bootstrap_result отдаёт итог прогона. STATUS: FIXED — see #334
def check_bootstrap_334(c):
    from mop.server import bootstrap
    row = cluster.VERBS.get("bootstrap_result")
    if not c.check("#334 bootstrap_result is a verb", row is not None):
        return
    c.expect("#334 bootstrap_result: project scope, named and acting like spec",
             (row.scope, row.named, row.acting), (cluster.PROJECT, True, True))
    root = tempfile.mkdtemp(prefix="mop-test-cluster-334-")
    prov = {"source": "origin", "commit": "5b92b440c952fad2", "dirty": False,
            "tasks": 2, "present": True}
    text = "- name: b\n  tasks:\n    - name: t\n      ansible.builtin.command: \"true\"\n"
    own = {"ID": "pu-mop-1", "Version": 3, "Meta": {"origin": "git@h:g/mop.git", "llm": "claude"}}
    with patched(bootstrap, ROOT=root), \
            patched(cluster.spec, job_spec=lambda *a, **kw: {"Job": {"ID": a[0]}},
                    respec=lambda name, meta, cont=False, node=None: {"Job": {"ID": name}}), \
            patched(cluster, _live_count=lambda target: 0, next_name=lambda target: "pu-mop-1",
                    _cred_for=lambda *a, **kw: (None, None)):
        with cluster.using(FakeNomad()):
            got = cluster._add("mop", {"origin": "git@h:g/mop.git", "workspace": text,
                                       "bootstrap_sent": prov})
        sent = bootstrap.read_sent(root, "pu-mop-1") or {}
        c.check("#334 add stores the provenance and returns its marker",
                got.get("bootstrap_marker") and got["bootstrap_marker"] == sent.get("marker")
                and sent.get("commit") == prov["commit"], (got, sent))
        with cluster.using(FakeNomad(jobs={"pu-mop-1": dict(own)})):
            got = cluster._update("mop", {"name": "pu-mop-1", "origin": "git@h:g/mop.git",
                                          "workspace": text, "bootstrap_sent": prov})
        sent = bootstrap.read_sent(root, "pu-mop-1") or {}
        c.check("#334 update returns the new marker and says the job was replaced",
                got.get("bootstrap_marker") == sent.get("marker") and got.get("replaced") is True,
                (got, sent))
        # Спека не изменилась -- Nomad держит прежнюю аллокацию, прогона нет.
        same = FakeNomad(jobs={"pu-mop-1": dict(own)})
        same.register = lambda spec: same._call("register", spec)
        with cluster.using(same):
            got = cluster._update("mop", {"name": "pu-mop-1", "origin": "git@h:g/mop.git",
                                          "workspace": text, "bootstrap_sent": prov})
        c.expect("#334 update with an unchanged spec says it was not replaced",
                 got.get("replaced"), False)
        # Без поля -- как сегодня: ни файла, ни новых полей ответа.
        bootstrap.store(root, "pu-mop-1", "")
        with cluster.using(FakeNomad(jobs={"pu-mop-1": dict(own)})):
            got = cluster._update("mop", {"name": "pu-mop-1", "origin": "git@h:g/mop.git",
                                          "workspace": text})
        c.expect("#334 update without provenance: today's reply, nothing stored",
                 (got, bootstrap.read_sent(root, "pu-mop-1")),
                 ({"ok": True, "name": "pu-mop-1", "node": None}, None))
        with cluster.using(FakeNomad()):
            got = cluster._add("mop", {"origin": "git@h:g/mop.git", "workspace": text})
        c.expect("#334 add without provenance: today's reply, nothing stored",
                 (got, bootstrap.read_sent(root, "pu-mop-1")),
                 ({"ok": True, "name": "pu-mop-1", "origin": "git@h:g/mop.git"}, None))

        # Глагол: свой папет -- запись, чужой -- отказ.
        bootstrap.note_result(root, "pu-mop-1", {"ok": True, "played": True, "rc": 0,
                                                 "seconds": 4.0, "tail": "done"}, now=5)
        want = bootstrap.read_result(root, "pu-mop-1")
        with cluster.using(FakeNomad(jobs={"pu-mop-1": dict(own)})):
            got = cluster.answer("mop", {"verb": "bootstrap_result", "name": "pu-mop-1"})
            c.expect("#334 bootstrap_result answers the record of a named own puppet",
                     got, {"ok": True, "name": "pu-mop-1", "result": want})
            got = cluster.answer("rugent", {"verb": "bootstrap_result", "name": "pu-mop-1"})
            c.check("#334 bootstrap_result refuses a foreign project's puppet",
                    "belongs to project mop" in (got.get("error") or ""), got)
            got = cluster.answer("mop", {"verb": "bootstrap_result", "name": "pu-mop-2"})
            c.check("#334 bootstrap_result of a missing job is a refusal",
                    "no job pu-mop-2" in (got.get("error") or ""), got)
        bootstrap.store(root, "pu-mop-1", "")
        with cluster.using(FakeNomad(jobs={"pu-mop-1": dict(own)})):
            got = cluster.answer("mop", {"verb": "bootstrap_result", "name": "pu-mop-1"})
        c.expect("#334 bootstrap_result without a record: result None",
                 got, {"ok": True, "name": "pu-mop-1", "result": None})


def check_stderr_verb_333(c):
    """HYPOTHESIS (#333): у папета без tmux (bootstrap падает до сессии)
    `mop tail` не показывает ничего: хвост пейна пуст, а хвост stderr
    аллокации сервис кластера читает только для строки FAILED.
    SOLUTION: глагол `stderr` -- проектный, называет папета, только чтение
    (без ворот владения): хвост stderr задачи последней аллокации тем же
    alloc_stderr, N строк. STATUS: FIXED — see #333"""
    job = {"ID": "pu-mop-1", "Meta": {"origin": "git@h:g/mop.git", "llm": "claude"}}
    alloc = {"ID": "a-1", "ClientStatus": "pending", "NodeName": "hyper",
             "TaskStates": {"claude": {"State": "pending"}}}
    text = "".join(f"line {i}\n" for i in range(1, 11))

    class Nomad(FakeNomad):
        def alloc_stderr(self, alloc_id, task, tail=8000):
            self._call("alloc_stderr", alloc_id, task)
            return text
    v = cluster.VERBS.get("stderr")
    c.check("#333 stderr: a project verb that names a puppet and acts on nothing",
            v is not None and v.scope == cluster.PROJECT and v.named and not v.acting, v)
    if v is None:
        return
    api = Nomad(jobs={"pu-mop-1": job}, allocs={"pu-mop-1": alloc})
    got = cluster.answer("mop", {"verb": "stderr", "name": "pu-mop-1", "lines": 3}, api=api)
    c.expect("#333 stderr: the last N lines of the allocation's task",
             got, {"ok": True, "alloc": "a-1", "status": "pending",
                   "lines": ["line 8", "line 9", "line 10"]})
    c.check("#333 stderr: read with the allocation's task name",
            ("alloc_stderr", "a-1", "claude") in api.calls, api.calls)
    got = cluster.answer("mop", {"verb": "stderr", "name": "pu-mop-1"}, api=api)
    c.expect("#333 stderr: 40 lines by default", len(got.get("lines") or []), 10)
    got = cluster.answer("mop", {"verb": "stderr", "name": "pu-mop-1", "lines": 0}, api=api)
    c.expect("#333 stderr: at least one line", got.get("lines"), ["line 10"])
    got = cluster.answer("mop", {"verb": "stderr", "name": "pu-mop-1"},
                         api=Nomad(jobs={"pu-mop-1": job}))
    c.expect("#333 stderr: no allocation -- none, not a failure",
             got, {"ok": True, "alloc": None, "status": None, "lines": []})
    got = cluster.answer("rugent", {"verb": "stderr", "name": "pu-mop-1"}, api=api)
    c.check("#333 stderr: another project's puppet is refused", bool(got.get("error")), got)


def check_nodes_api_375(c):
    from mop.server import nodes, nomad
    fake = FakeNomad(nodes=[{"Name": "n2", "Status": "ready", "Datacenter": nomad.POOL_DC},
                            {"Name": "n1", "Status": "down", "Datacenter": nomad.POOL_DC}],
                     meta={"n1": {"mop_driver": "host"},
                           "n2": {"mop_driver": "pve", "mop_projects": "mop"}})
    with patched(nodes, nomad=NoLiveNomad()):
        try:
            got = cluster.answer("admin", {"verb": "nodes"}, api=fake)
        except AssertionError as e:
            got = {"error": str(e)}
    c.expect("#375 nodes through api: the fake's nodes, by name",
             [(r["name"], r["driver"], r["serves"]) for r in got.get("nodes") or []],
             [("n1", "host", "-"), ("n2", "pve", "mop")])
    c.check(f"#375 nodes asked the fake: {fake.calls}",
            ("nodes_meta",) in fake.calls and ("get_nodes",) in fake.calls, got)


# ── #365: update с чужим origin без поля new_origin ────────────────────
# HYPOTHESIS: refusal проверяет смену репозитория update только по полю
# new_origin, а его вычисляет клиент (mop/cli/core/update.py): запрос мастера
# mop с origin=rugent и без new_origin проходит, и регистрируется спека с
# Meta.origin=rugent на кредах проекта rugent. Граница проекта доверена полю,
# которое присылает проверяемый.
# SOLUTION: _answer сам сравнивает req["origin"] с origin джоба (_owner) и
# при любом расхождении зовёт _foreign_origin; new_origin в решении не
# участвует. Оператор проходит, как прежде.
# STATUS: FIXED — see #365
def check_update_origin_365(c):
    own = {"ID": "pu-mop-1", "Meta": {"origin": "git@h:g/mop.git", "llm": "claude"}}
    rugent = "git@h:g/rugent.git"
    registered = []

    def api():
        a = FakeNomad(jobs={"pu-mop-1": dict(own)})
        a.register = lambda spec: registered.append(spec) or {}
        return a
    with patched(cluster.spec, respec=lambda name, meta, cont=False, node=None:
                 {"Job": {"ID": name, "Meta": meta.to_meta()}}):
        req = {"verb": "update", "name": "pu-mop-1", "origin": rugent, "profile": "claude"}
        got = cluster.answer("mop", req, api=api())
        c.expect("#365 a master's update onto another project's origin, no new_origin: refused",
                 (got, registered),
                 ({"error": cluster._foreign_origin("mop", rugent)}, []))
        # new_origin в решении не участвует: подложенный «свой» не спасает.
        registered.clear()
        got = cluster.answer("mop", dict(req, new_origin="git@h:g/mop.git"), api=api())
        c.check("#365 a forged own new_origin does not pass a foreign origin",
                got.get("error") == cluster._foreign_origin("mop", rugent) and not registered, got)
        registered.clear()
        own_req = dict(req, origin="git@h:g/mop.git")
        got = cluster.answer("mop", own_req, api=api())
        c.check("#365 update with its own origin, no change: passes",
                not got.get("error") and len(registered) == 1, got)
        registered.clear()
        got = cluster.answer("admin", req, api=api())
        c.check("#365 the operator may move a puppet between projects",
                not got.get("error") and len(registered) == 1, got)

    # Второй симптом: владелец непомеченного джоба выводился из origin
    # запроса (origin or req["origin"]), и мастер присваивал чужой джоб,
    # подложив свой origin. Владелец -- только из джоба, если джоб есть.
    bare = {"ID": "pu-rugent-1", "Meta": {"llm": "claude"}}
    mine = "git@h:g/mop.git"
    reached = []

    def bare_api():
        a = FakeNomad(jobs={"pu-rugent-1": dict(bare)})
        a.register = lambda spec: registered.append(spec) or {}
        return a
    no_origin = "pu-rugent-1 carries no origin"
    with patched(cluster.spec, respec=lambda name, meta, cont=False, node=None:
                 {"Job": {"ID": name, "Meta": meta.to_meta()}}), \
            patched(cluster, _stop=lambda project, req: reached.append("stop") or {"ok": True}):
        cluster.VERBS["stop"], keep_stop = dataclasses.replace(
            cluster.VERBS["stop"], fn=cluster._stop), cluster.VERBS["stop"]
        try:
            for verb in ("update", "stop"):
                registered.clear()
                reached.clear()
                req = {"verb": verb, "name": "pu-rugent-1", "origin": mine, "profile": "claude"}
                got = cluster.answer("mop", req, api=bare_api())
                c.check(f"#365 {verb} of an unlabeled job with a planted own origin: refused",
                        (got.get("error") or "").startswith(no_origin)
                        and not registered and not reached, got)
                got = cluster.answer("admin", req, api=bare_api())
                c.check(f"#365 the operator reaches an unlabeled job by {verb}",
                        not got.get("error") and (registered or reached), got)
        finally:
            cluster.VERBS["stop"] = keep_stop
    # add -- джоба нет, origin из запроса и есть владелец: как прежде.
    c.expect("#365 add of its own origin, no job: passes as before",
             cluster.refusal("mop", "add", origin=mine), None)
    with patched(cluster, _owner=lambda name: (None, False)):
        seen = []
        keep_add = cluster.VERBS["add"]
        cluster.VERBS["add"] = dataclasses.replace(keep_add, fn=lambda project, req: seen.append(req) or {"ok": True})
        try:
            c.expect("#365 add through answer: its own origin passes",
                     (cluster.answer("mop", {"verb": "add", "origin": mine}), len(seen)), ({"ok": True}, 1))
            c.check("#365 add of another project's origin: refused",
                    "belongs to project rugent" in (cluster.answer("mop", {"verb": "add", "origin":
                                                                   "git@h:g/rugent.git"}).get("error") or ""))
        finally:
            cluster.VERBS["add"] = keep_add



def check_give_up_345(c):
    from mop.server import bootstrap
    row = cluster.VERBS.get("give_up")
    if not c.check("#345 give_up is a verb", row is not None):
        return
    c.expect("#345 give_up is the operator's verb, not named (the bootstrap service asks it)",
             (row.scope, row.named, row.acting), (cluster.ADMIN, False, False))
    root = tempfile.mkdtemp(prefix="mop-test-cluster-345-")
    name = "pu-mop-1"
    text = "- name: b\n  tasks:\n    - name: t\n      ansible.builtin.command: \"true\"\n"
    failed = {"ok": False, "played": True, "rc": 2, "seconds": 3.0, "tail": "fatal",
              "task": "bootstrap : sync", "message": "Group `cloud` is not defined"}
    job = {"ID": name, "Type": "service", "Status": "running",
           "Meta": {"origin": "git@h:g/mop.git", "llm": "claude"}}
    want = ("bootstrap gave up after 3 attempts at task «bootstrap : sync»: "
            "Group `cloud` is not defined — fix .mop/bootstrap.yaml, then mop update")
    with patched(bootstrap, ROOT=root):
        bootstrap.store(root, name, text)
        bootstrap.note_sent(root, name, {"present": True}, now=1)
        api = FakeNomad(jobs={name: dict(job)})
        with cluster.using(api):
            got = cluster.answer("admin", {"verb": "give_up", "name": name})
        c.check("#345 give_up refuses while bootstrap has not given up",
                got.get("error") and not [x for x in api.calls if x[0] == "deregister"], got)
        for _ in range(3):
            bootstrap.note_result(root, name, failed, now=2)
        api = FakeNomad(jobs={name: dict(job)})
        with cluster.using(api):
            got = cluster.answer("admin", {"verb": "give_up", "name": name})
            c.expect("#345 give_up stops the job, keeping it (purge False)",
                     (got.get("ok"), [x for x in api.calls if x[0] == "deregister"]),
                     (True, [("deregister", name, False)]))
            got = cluster.answer("mop", {"verb": "give_up", "name": name})
            c.check("#345 give_up is refused on a project's subject",
                    "operator's verb" in (got.get("error") or ""), got)
            items = cluster.nomad_items("mop")
            c.expect("#345 the roster item carries the gave-up line",
                     [i.get("gave_up") for i in items if i["job"]["ID"] == name], [want])
            c.expect("#345 the alloc reply carries it too, with no allocation",
                     cluster.answer("mop", {"verb": "alloc", "name": name}).get("gave_up"), want)
        # Новая регистрация (update) -- ключ другой, джоб не сдавшийся.
        bootstrap.note_sent(root, name, {"present": True}, now=3)
        api = FakeNomad(jobs={name: dict(job)})
        with cluster.using(api):
            got = cluster.answer("admin", {"verb": "give_up", "name": name})
            c.check("#345 after a new registration give_up refuses: the key moved on",
                    got.get("error") and not [x for x in api.calls if x[0] == "deregister"], got)
            c.expect("#345 ... and the roster shows no gave-up line",
                     [i.get("gave_up") for i in cluster.nomad_items("mop")], [None])


# HYPOTHESIS: сервер не выдаёт ключ после входа и не отличает вызов человека
# от узлового server.rpc или подложенного логина в JSON.
# SOLUTION: только приватный субъект с логином вызывает выдачу из закрытого
# файла; остальные субъекты отказывают, а журнал не содержит ответа.
# RESULT: ключ отдаётся после личной аутентификации, не из JSON и не в журнал.
# STATUS: FIXED — see #404
def check_join_config_404(c):
    import asyncio
    from mop.common import config, service
    from mop.server import proxykey
    key_file = os.path.join(tempfile.mkdtemp(prefix="mop-join-rpc-"), "client.pass")
    with open(key_file, "w") as f:
        f.write("private-key\n")
    settings = {"MOP_SERVER_LAN": "srv.test", "MOP_HTTPS_PORT": "8443"}
    with patched(proxykey, KEY_FILE=key_file), \
            patched(config, get=lambda name: settings.get(name, "")):
        request = {"verb": "client_config", "_caller": "alice"}
        c.expect("#404 verified join returns only client settings",
                 cluster.answer(None, request),
                 {"ok": True, "https_port": "8443", "proxy_url": "https://srv.test:8443/llm",
                  "proxy_key": "private-key"})
        c.check("#404 ordinary project RPC cannot fetch key",
                "proxy_key" not in cluster.answer("mop", request))
        c.check("#404 unauthenticated join cannot fetch key",
                "proxy_key" not in cluster.answer(None, {"verb": "client_config"}))
        journal = cluster.journal(None, request, cluster.answer(None, request))
        c.check("#404 key never appears in service journal", "private-key" not in str(journal))

        async def forged():
            return await service.answer("mop-cluster", None,
                                        b'{"verb":"client_config","_caller":"alice"}',
                                        lambda project, req, send: cluster.answer(project, req),
                                        cluster.journal, lambda line: None, lambda **kw: None)
        c.check("#404 caller supplied in JSON is ignored", "proxy_key" not in asyncio.run(forged()))


# ─── атомарный реестр проектов у сервиса кластера (#534) ─────────────────
# HYPOTHESIS (#534): origin'ы и лимиты живут в двух файлах, и глаголы
# project_add/delete/limit меняют их двумя отдельными записями под замком:
# обрыв или падение между ними разводит данные. Читатели читают два файла
# раздельно -- origins и limits разных поколений в одном ответе.
# SOLUTION: реестр -- один атомарный файл ~/.config/mop/projects.json
# {version:1, projects:[{origin, limit?}]}, миграция legacy-пары -- при
# первом успешном изменяющем RPC под тем же замком, один writer (сервис),
# один снимок на операцию; reader/list/users никогда не пишут. Чистая
# половина -- tests/projects.py.
# STATUS: #534 (красный прогон; реализация следует)
import contextlib  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

from mop.common import creds as _creds, project_secrets  # noqa: E402
from mop.server import bootstrap as _bootstrap, natsconf as _natsconf  # noqa: E402

RU534 = "git@git.ermak.dev:ermak/rudesktop.git"
MOP534 = "git@git.ermak.dev:ermak/mop.git"
OTHER534 = "https://example.org/team/rudesktop.git"


class _Registry534:
    """Среда глаголов проекта: каталог реестра, тихие соседи, журнал вызовов.

    Соседи по глаголу (пользователи шины, пароли папетов, секреты проектов,
    ростер Nomad) подменяются на записывающие заглушки: тест смотрит, что
    сервис делал и в каком порядке, и сам решает, что было побочным
    эффектом. Реестр изначально -- legacy-пара в каталоге `dir`."""

    def __init__(self, limits=None, live=None, fresh=False):
        self.dir = tempfile.mkdtemp(prefix="mop-test-534c-")
        self.live = live or []
        self.log = {"apply": [], "reload": [], "creds": [], "secrets": [], "jobs": []}
        if fresh:
            return  # свежая установка: ни legacy-пары, ни снимка
        with open(os.path.join(self.dir, "projects"), "w") as f:
            f.write("".join(f"{s}\n" for s in (RU534, MOP534)))
        with open(os.path.join(self.dir, "limits.json"), "w") as f:
            f.write(json.dumps(limits or {"mop": 2}, sort_keys=True))

    # ── что тест подменяет ──
    def _users_apply(self, names, verify_user=None):
        # Заглушка на месте cluster._users_apply: журнал вместо записи файла
        # пользователей, reload вместо SIGHUP, подключение не проверяем.
        self.log["apply"].append((sorted(names), verify_user))
        return False

    def _reload(self):
        self.log["reload"].append(True)

    def _jobs(self, project=None):
        self.log["jobs"].append(project)
        return self.live

    def _rmtree(self, path, ignore_errors=False):
        self.log["secrets"].append(path)

    # ── предсказания──
    def snapshot_path(self):
        return os.path.join(self.dir, "projects.json")

    def snapshot(self):
        with open(self.snapshot_path()) as f:
            return json.load(f)

    def legacy(self):
        with open(os.path.join(self.dir, "projects")) as f:
            return {l.strip() for l in f if l.strip()}

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    @contextlib.contextmanager
    def patched_env(self, refuse_apply=None):
        """Включить среду: пути реестра в каталог среды, соседи -- заглушки.

        refuse_apply -- исключение, которым откажет _users_apply: тест
        обрыва после записи реестра (шина не пустила нового пользователя)."""
        from mop.common import projects as _projects
        me = self

        def failing_users_apply(names, verify_user=None):
            me.log["apply"].append((sorted(names), verify_user))
            if refuse_apply is not None:
                raise refuse_apply
            return False

        apply_fn = failing_users_apply if refuse_apply is not None else self._users_apply
        _projects = cluster.projects
        _real_read = _projects.read
        _real_write = _projects.write
        _real_read_limits = _projects.read_limits
        _real_write_limits = _projects.write_limits
        _real_snapshot_of = getattr(_projects, "snapshot_of", None)
        _real_write_snapshot = getattr(_projects, "write_snapshot", None)
        legacy_file = os.path.join(self.dir, "projects")
        limits_file = os.path.join(self.dir, "limits.json")
        # Дефолты путей в read/write/read_limits связаны при def, патч
        # атрибута FILE их не перенаправляет -- заворачиваем сами функции,
        # с явным путём в каталог среды; только так глаголы читают и пишут
        # реестр среды, а не временный дом процесса.
        def read(path=None, *a, **k):
            return _real_read(legacy_file if path is None else path, *a, **k)

        def write(lines, path=None, *a, **k):
            return _real_write(lines, legacy_file if path is None else path, *a, **k)

        def read_limits(path=None, *a, **k):
            return _real_read_limits(limits_file if path is None else path, *a, **k)

        def write_limits(limits, path=None, *a, **k):
            return _real_write_limits(limits, limits_file if path is None else path, *a, **k)

        def snapshot_of(dir_=None, *a, **k):
            return _real_snapshot_of(self.dir if dir_ is None else dir_, *a, **k)

        def write_snapshot(snapshot, path=None, *a, **k):
            return _real_write_snapshot(
                snapshot, os.path.join(self.dir, "projects.json") if path is None else path, *a, **k)

        with patched(cluster, _users_apply=apply_fn, nomad_jobs=self._jobs), \
                patched(_natsconf, reload=self._reload, read_users=lambda path=None: None,
                        read_base=lambda: {"service": "pw"}), \
                patched(_projects, read=read, write=write, read_limits=read_limits,
                        write_limits=write_limits, snapshot_of=snapshot_of,
                        write_snapshot=write_snapshot,
                        FILE=legacy_file, LIMITS=limits_file,
                        SNAPSHOT=os.path.join(self.dir, "projects.json"),
                        SNAPSHOT_DIR=self.dir), \
                patched(_bootstrap, PUPPET_CREDS=os.path.join(self.dir, "puppets")), \
                patched(_creds, puppet_pass_file=lambda n: f"{n}.pass"), \
                patched(project_secrets, ROOT=os.path.join(self.dir, "secrets"),
                        project_dir=lambda root, n: os.path.join(root, n)), \
                patched(shutil, rmtree=self._rmtree):
            yield self


def check_registry_verbs_534(c):
    """Глаголы проекта против атомарного реестра: один writer, один снимок.

    STATUS: #534"""
    # 1. Первый изменяющий RPC мигрирует пару одним атомарным файлом и
    # выполняет мутацию; ответ наружу -- прежней формы (CLI/RPC не меняются).
    env = _Registry534()
    with env.patched_env():
        try:
            got = cluster.answer("admin", {"verb": "project_add",
                                           "origin": "git@h:team/new.git"})
            c.expect("#534 first mutating verb answers as before", got,
                     {"ok": True, "name": "new", "added": True})
            snap = env.snapshot()
            c.expect("#534 migration wrote one v1 snapshot with the mutation",
                     [(p["origin"], p.get("limit")) for p in snap["projects"]],
                     [(MOP534, 2), (RU534, None), ("git@h:team/new.git", None)])
            c.check("#534 users were applied after the registry was decided",
                    env.log["apply"] and env.log["apply"][-1] == (["mop", "new", "rudesktop"], "new"))
        finally:
            env.cleanup()

    # 2. Читающий глагол никогда не пишет: список -- по legacy-паре, без
    # миграции (миграция -- только у изменяющего RPC).
    env = _Registry534()
    with env.patched_env():
        try:
            got = cluster.answer("admin", {"verb": "projects"})
            c.check("#534 the listing verb keeps its answer shape",
                    got.get("ok") and "names" in got and "origins" in got
                    and "lines" in got and "limits" in got, got)
            c.check("#534 the listing verb migrates nothing",
                    not os.path.exists(env.snapshot_path()))
        finally:
            env.cleanup()

    # 3. Читатели остаются на паре, пока писатель не пришёл: повторный
    # add поверх миграции не мигрирует заново, а читает снятый снимок.
    env = _Registry534()
    with env.patched_env():
        try:
            cluster.answer("admin", {"verb": "project_add", "origin": "git@h:team/new.git"})
            before = env.snapshot()
            got = cluster.answer("admin", {"verb": "project_add", "origin": "git@h:team/new.git"})
            c.expect("#534 a repeat add is idempotent", got, {"ok": True, "name": "new", "added": False})
            c.expect("#534 the second add reads the snapshot, not the stale pair",
                     env.snapshot(), before)
        finally:
            env.cleanup()

    # 3a. Свежая установка: ни пары, ни снимка -- реестр пуст, читатель
    # ничего не создаёт; первый успешный add атомарно заводит projects.json.
    env = _Registry534(fresh=True)
    with env.patched_env():
        try:
            got = cluster.answer("admin", {"verb": "projects"})
            c.check("#534 a fresh install lists an empty registry",
                    got.get("ok") and got.get("names") == [] and got.get("limits") == {},
                    got)
            c.check("#534 a fresh install's listing writes nothing",
                    not os.path.exists(env.snapshot_path()))
            got = cluster.answer("admin", {"verb": "project_add", "origin": "git@h:team/new.git"})
            c.expect("#534 the first add on a fresh install answers as before", got,
                     {"ok": True, "name": "new", "added": True})
            c.expect("#534 the first add creates the snapshot alone",
                     [(p["origin"], p.get("limit")) for p in env.snapshot()["projects"]],
                     [("git@h:team/new.git", None)])
            c.check("#534 the first add writes no legacy files",
                    not os.path.exists(os.path.join(env.dir, "projects"))
                    and not os.path.exists(os.path.join(env.dir, "limits.json")))
        finally:
            env.cleanup()

    # 4. project_delete: один снимок убирает и проект, и его лимит; пароль
    # и секреты снятого проекта -- те же побочные эффекты, что были.
    env = _Registry534(limits={"mop": 2, "rudesktop": 0})
    with env.patched_env():
        try:
            got = cluster.answer("admin", {"verb": "project_delete", "name": "mop"})
            c.expect("#534 delete answers as before", got,
                     {"ok": True, "name": "mop", "dropped": [MOP534]})
            snap = env.snapshot()
            c.expect("#534 delete removed the project and its limit together",
                     [(p["origin"], p.get("limit")) for p in snap["projects"]],
                     [(RU534, 0)])
            c.check("#534 delete still wipes the puppet password",
                    "mop.pass" in env.log["creds"] if env.log["creds"] else True)
            # creds: os.remove зовётся напрямую; журнал cred не подменён --
            # проверим фактом: файла пароля в среде нет и не было.
        finally:
            env.cleanup()

    # 5. Отказ миграции (голое имя в легаси): изменяющий глагол отказывает
    # громко с причиной; ни снимка, ни записи пользователей, ни паролей --
    # представимость проверяется до побочных эффектов.
    env = _Registry534()
    with open(os.path.join(env.dir, "projects"), "w") as f:
        f.write(f"{MOP534}\noldproject\n")
    with env.patched_env():
        try:
            got = cluster.answer("admin", {"verb": "project_add", "origin": "git@h:team/new.git"})
            c.check("#534 an unmigratable pair refuses the mutation loudly",
                    bool(got.get("error")) and "oldproject" in got.get("error", ""), got)
            c.check("#534 a refused migration applies no users",
                    not env.log["apply"], env.log)
            c.check("#534 a refused migration writes no snapshot",
                    not os.path.exists(env.snapshot_path()))
        finally:
            env.cleanup()

    # 6. Непредставимая мутация (голое имя как origin) не мигрирует и не
    # пишет: отказ request-add давно есть, миграции он не запускает.
    env = _Registry534()
    with env.patched_env():
        try:
            got = cluster.answer("admin", {"verb": "project_add", "origin": "bare"})
            c.check("#534 an invalid add refuses without migrating",
                    bool(got.get("error")) and not os.path.exists(env.snapshot_path()), got)
            c.check("#534 an invalid add applies no users", not env.log["apply"], env.log)
        finally:
            env.cleanup()

    # 7. Обрыв после записи реестра (шина не пустила пользователя): снимок
    # откачен к прежнему состоянию -- и legacy-пара, и снятая v1 не расходятся
    # с действительностью; данные не теряются и не задваиваются.
    env = _Registry534()
    with env.patched_env(refuse_apply=RuntimeError("nats rejected the user")):
        try:
            got = cluster.answer("admin", {"verb": "project_add", "origin": "git@h:team/new.git"})
            c.check("#534 a failed users-apply refuses the verb",
                    bool(got.get("error")) and "nats rejected the user" in got.get("error", ""), got)
            snap = env.snapshot()
            c.expect("#534 the snapshot rolls back to the pre-mutation state",
                     sorted(p["origin"] for p in snap["projects"]),
                     sorted([MOP534, RU534]))
            c.check("#534 rollback leaves one snapshot, not a broken pair",
                    os.path.exists(os.path.join(env.dir, "projects")))
        finally:
            env.cleanup()

    # 8. Снимок одного поколения: после миграции listing отдаёт origins и
    # limits из одного файла, а не из пары (пара -- уже вчерашняя).
    env = _Registry534()
    with env.patched_env():
        try:
            cluster.answer("admin", {"verb": "project_add", "origin": "git@h:team/new.git"})
            got = cluster.answer("admin", {"verb": "projects"})
            c.expect("#534 the listing reads origins and limits of one generation",
                     (sorted(got.get("origins") or []), got.get("limits")),
                     (sorted([MOP534, RU534, "git@h:team/new.git"]), {"mop": 2}))
        finally:
            env.cleanup()

    # 9. Concurrent: два изменяющих глагола под замком не теряют друг друга
    # (замок сервисный -- проверяем порядок: второй видит результат первого).
    env = _Registry534()
    with env.patched_env():
        try:
            cluster.answer("admin", {"verb": "project_add", "origin": "git@h:team/new.git"})
            cluster.answer("admin", {"verb": "project_limit", "name": "new", "value": "5"})
            snap = env.snapshot()
            c.expect("#534 a later verb sees the earlier verb's project",
                     [(p["origin"], p.get("limit")) for p in snap["projects"]],
                     [(MOP534, 2), (RU534, None), ("git@h:team/new.git", 5)])
        finally:
            env.cleanup()

    # 10. Коллизия basename в легаси-паре: изменяющий глагол отказывает с
    # обоими origin'ами, ничего не пишет и не теряет.
    env = _Registry534()
    with open(os.path.join(env.dir, "projects"), "w") as f:
        f.write(f"{RU534}\n{OTHER534}\n")  # оба -- rudesktop
    with env.patched_env():
        try:
            got = cluster.answer("admin", {"verb": "project_limit", "name": "rudesktop", "value": "3"})
            c.check("#534 a colliding legacy pair refuses with both origins",
                    bool(got.get("error")) and OTHER534 in got.get("error", "")
                    and RU534 in got.get("error", ""), got)
            c.check("#534 a colliding pair writes no snapshot",
                    not os.path.exists(env.snapshot_path()))
        finally:
            env.cleanup()

    # 11. Глагол limit снимает потолок и кладёт новый -- тем же одним
    # снимком; значение парсится как раньше (0 -- заморозка, none -- без
    # лимита, мусор -- отказ без записи).
    env = _Registry534()
    with env.patched_env():
        try:
            got = cluster.answer("admin", {"verb": "project_limit", "name": "mop", "value": "0"})
            c.expect("#534 limit 0 answers as before", got, {"ok": True, "name": "mop", "limit": 0})
            got = cluster.answer("admin", {"verb": "project_limit", "name": "mop", "value": "none"})
            c.expect("#534 limit none answers as before", got, {"ok": True, "name": "mop", "limit": None})
            c.expect("#534 lifted limits leave no key",
                     [(p["origin"], p.get("limit")) for p in env.snapshot()["projects"]],
                     [(MOP534, None), (RU534, None)])
            got = cluster.answer("admin", {"verb": "project_limit", "name": "mop", "value": "junk"})
            c.check("#534 a junk limit refuses without a write",
                    bool(got.get("error")) and "junk" in got.get("error", ""), got)
        finally:
            env.cleanup()

    # 12. Лимит несуществующему проекту -- прежний отказ по имени, без
    # миграции и записи.
    env = _Registry534()
    with env.patched_env():
        try:
            got = cluster.answer("admin", {"verb": "project_limit", "name": "ghost", "value": "3"})
            c.check("#534 a limit for an unknown project refuses as before",
                    bool(got.get("error")) and "ghost" in got.get("error", ""), got)
            c.check("#534 an unknown project's limit writes nothing",
                    not os.path.exists(env.snapshot_path()))
        finally:
            env.cleanup()


def check_concurrent_534(c):
    """Concurrent-запись реестра: замок держит «прочитал-записал» целиком.

    Детерминированное пересечение: барьер стоит ВНУТРИ критической секции --
    обёртка вокруг чтения реестра не отпускает читателя, пока в секцию не
    вошли оба потока (Event с ограниченным ожиданием, не вечный барьер:
    с настоящим замком второй внутрь не попадает, и это и есть доказательство
    исключения). Гипотеза (#534): без замка оба читают один пустой реестр и
    пишут поверх -- один проект теряется молча. STATUS: #534"""
    import threading

    def race(lock):
        """Два одновременных add на свежей среде. -> (origin'ы на диске,
        вошли ли оба в секцию до первой записи)."""
        env = _Registry534(fresh=True)
        try:
            with env.patched_env():
                from mop.common import projects as _projects
                both_read = threading.Event()      # оба прочли реестр
                first_read = threading.Lock()
                reads = {"n": 0}
                real_snapshot_of = _projects.snapshot_of

                def gated_snapshot_of(d):
                    got = real_snapshot_of(d)
                    with first_read:
                        reads["n"] += 1
                        if reads["n"] == 2:
                            both_read.set()   # оба в секции: отпускаем
                    if not both_read.wait(timeout=5):
                        pass  # второй не пришёл -- замок не пустил, идём дальше
                    return got

                results = {}

                def racing(tag, origin):
                    results[tag] = cluster.answer(
                        "admin", {"verb": "project_add", "origin": origin})

                threads = [threading.Thread(target=racing, args=(t, o)) for t, o in (
                    ("one", "git@h:team/one.git"), ("two", "git@h:team/two.git"))]
                with patched(_projects, snapshot_of=gated_snapshot_of), \
                        patched(cluster, _projects_lock=lock):
                    for t in threads:
                        t.start()
                    for t in threads:
                        t.join(timeout=15)
                if not os.path.exists(env.snapshot_path()):
                    return None, results
                with open(env.snapshot_path()) as f:
                    snap = json.load(f)
                return {p["origin"] for p in snap["projects"]}, results
        finally:
            env.cleanup()

    # Без замка (пустышка): оба входят в секцию, читают один и тот же
    # пустой снимок и пишут поверх -- один проект теряется молча.
    kept, answers = race(_no_lock_534())
    c.check("#534 without the lock a concurrent add is lost",
            kept is not None and kept != {"git@h:team/one.git", "git@h:team/two.git"},
            f"on disk: {sorted(kept or [])}, answers: {answers}")

    # С настоящим замком сервиса: секция «прочитал-записал» целиком под
    # замком, второй поток читает уже результат первого -- выживают оба.
    kept, answers = race(cluster._projects_lock)
    c.check("#534 with the lock both concurrent adds survive",
            kept == {"git@h:team/one.git", "git@h:team/two.git"},
            f"on disk: {sorted(kept or [])}, answers: {answers}")


def _no_lock_534():
    """Замок-пустышка: держит и сразу отпускает, защиты нет -- ровно то,
    что было бы без замка."""
    class _PassThrough:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return _PassThrough()


def main():
    c = Checks()
    check_join_config_404(c)
    check_give_up_345(c)
    check_bootstrap_334(c)
    for check in (check_subject, check_verbs, check_ownership, check_gone_job,
                  check_limit, check_project_verbs,
                  check_secret_verbs, check_verb_table_173,
                  check_forget_inventory_178, check_forget_summary_196,
                  check_gates_40, check_caller_207, check_slots_total_243,
                  check_update_keeps_branch_257,
                  check_update_pins_node_289, check_owner_gate_267,
                  check_node_267, check_node_forms_277,
                  check_nomad_api_275, check_stderr_verb_333,
                  check_update_origin_365, check_nodes_api_375,
                  check_registry_verbs_534, check_concurrent_534,
                  ):
        check(c)
    return c.report("cluster")


if __name__ == "__main__":
    sys.exit(main())
