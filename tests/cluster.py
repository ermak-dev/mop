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

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, offline, patched, restored, GATE_NOW, gate_table_267  # noqa: E402
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
                got = {"reply": cluster.answer(project, {"verb": verb, "name": "pu-x-1",
                                                         "new_origin": new}),
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
    with offline(), restored(nomad, "latest_alloc"), restored(bus, "request"):
        calls = []
        nomad.latest_alloc = lambda name: {"ID": "a1", "NodeName": "n1"}
        bus.request = lambda node, verb, **k: calls.append((node, verb, k.get("name"))) \
            or {"clone": None}
        got = cluster._clone_of("pu-mop-1")
        c.check("_clone_of must ask the node's agent",
                not (got != {"clone": None} or calls != [("n1", "clone", "pu-mop-1")]),
                f"{got!r}, {calls}")
        nomad.latest_alloc = lambda name: None
        c.check("_clone_of without an allocation must be None",
                not (cluster._clone_of("pu-mop-1") is not None))

        def silent(node, verb, **k):
            raise bus.BusError("node agent n1 is not subscribed")
        nomad.latest_alloc = lambda name: {"ID": "a1", "NodeName": "n1"}
        bus.request = silent
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
    with restored(cluster.nomad, "get_job", "register"), restored(cluster.spec, "job_spec"), \
            restored(cluster, "store_workspace"):
        cluster.nomad.get_job = lambda name: {"ID": name, "Meta": {"origin": "git@h:g/mop.git",
                                                                   "llm": "claude", "branch": "swarm"}}
        cluster.nomad.register = lambda job: None
        cluster.spec.job_spec = lambda name, origin, profile=None, cont=False, branch=None: \
            calls.append(branch) or {"Job": {"ID": name}}
        cluster.store_workspace = lambda root, name, req: None
        cluster._update("mop", {"name": "pu-mop-1", "origin": "git@h:g/mop.git"})
        cluster._update("mop", {"name": "pu-mop-1", "origin": "git@h:g/mop.git", "branch": "dev"})
        cluster.nomad.get_job = lambda name: None
        cluster._update("mop", {"name": "pu-mop-1", "origin": "git@h:g/mop.git"})
    c.expect("update must keep the meta branch unless the request names one",
             calls, ["swarm", "dev", None])


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
    SOLUTION: domain.Node -- одно значение, каждая форма провода -- своей
    парой from_/to_, ключи и их порядок прежние (снимки те же); читатели
    идут через него. STATUS: FIXED — see #267"""
    import json
    from mop.common import domain, puppets, bus
    from mop.server import nodes
    Node = getattr(domain, "Node", None)
    if not c.check("domain.Node exists", not (Node is None)):
        return
    pool = [{"name": "a", "status": "down"},
            {"name": "b", "status": "ready", "error": "meta: boom"},
            {"name": "c", "status": "ready", "free_mb": 8192, "total_mb": 16384,
             "slots": 1, "slots_total": 2, "eligible": True},
            {"name": "d", "status": "ready", "free_mb": 0, "total_mb": 4096,
             "slots": 0, "slots_total": 0, "eligible": False}]
    for d in pool:
        back = Node.from_pool(d).to_pool()
        c.check("pool form round trip", not (json.dumps(back) != json.dumps(d)),
                f"{d} -> {back}")
    summaries = [({"Name": "c", "Status": "ready"}, {"mop_projects": "mop"}, pool[2]),
                 ({"Name": "e", "Status": "ready", "Drain": True}, {}, {}),
                 ({"Name": "f", "Status": "ready"}, {"mop_driver": "no-such"}, {})]
    for summary, meta, cap in summaries:
        row = nodes.row(summary, meta, cap)
        back = Node.from_row(row).to_row()
        c.check("nodes form round trip", not (json.dumps(back) != json.dumps(row)),
                f"{row} -> {back}")
    with restored(bus, "call_cluster"):
        bus.call_cluster = lambda verb, **kw: {"ok": True, "nodes": pool}
        got = puppets.pool()
        c.check("puppets.pool must give Node values",
                not (not all(isinstance(n, Node) for n in got)), repr(got))
        # Как было: ready и не закрытый планированию; у сломанного (error)
        # eligible нет, и он -- по умолчанию открыт.
        c.expect("ready_nodes: ready and not closed", puppets.ready_nodes(), {"b", "c"})
        rows = [nodes.row(*x) for x in summaries]
        bus.call_cluster = lambda verb, **kw: {"ok": True, "nodes": rows}
        got = puppets.nodes()
        c.check("puppets.nodes must give Node values of the same rows",
                not (not all(isinstance(n, Node) for n in got)
                     or [n.to_row() for n in got] != rows), repr(got))


def main():
    c = Checks()
    for check in (check_subject, check_verbs, check_ownership, check_gone_job,
                  check_limit, check_project_verbs,
                  check_secret_verbs, check_verb_table_173,
                  check_forget_inventory_178, check_forget_summary_196,
                  check_gates_40, check_caller_207, check_slots_total_243,
                  check_update_keeps_branch_257, check_owner_gate_267,
                  check_node_267):
        check(c)
    return c.report("cluster")


if __name__ == "__main__":
    sys.exit(main())
