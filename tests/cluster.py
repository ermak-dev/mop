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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import cluster, service  # noqa: E402

RUGENT = "git@git.ermak.dev:rugent/rugent.git"
MOP = "git@git.ermak.dev:ermak/mop.git"


def check_subject():
    """Проект — первый токен субъекта, и никогда не поле запроса."""
    out = []
    for subj, want in [("mop.rugent.cluster.rpc", "rugent"),
                       ("mop.admin.cluster.rpc", "admin"),
                       ("mop.mop.cluster.rpc", "mop")]:
        got = service.project_from_subject(subj)
        if got != want:
            out.append(f"project_of({subj}) -> {got!r}, wanted {want!r}")
    return out


def check_verbs():
    """Набор закрыт, и узловые глаголы — только оператору."""
    out = []
    if cluster.refusal("rugent", "nosuchverb") is None:
        out.append("an unknown verb must be refused, not attempted")

    # Узел общий для всех жильцов: увести чужого папета с машины или выкинуть
    # её из ростера — не дело мастера проекта.
    for verb in ("drain", "up", "forget", "meta", "nodes"):
        if cluster.refusal("rugent", verb) is None:
            out.append(f"{verb} must be an operator's verb, refused for a project")
        if cluster.refusal("admin", verb) is not None:
            out.append(f"{verb} must work for the operator: "
                       f"{cluster.refusal('admin', verb)}")

    # ping отвечает всем: мастеру надо уметь отличить «сервис лёг» от
    # «Nomad не отвечает», не имея под рукой ни того, ни другого.
    for who in ("rugent", "admin"):
        if cluster.refusal(who, "ping") is not None:
            out.append(f"ping must answer {who}")

    # Оператор делает и проектные глаголы: он и сегодня удаляет любого папета.
    if cluster.refusal("admin", "delete", origin=RUGENT) is not None:
        out.append("the operator must reach any project's puppet")
    return out


def check_ownership():
    """Дыра, ради которой заводился сервис: чужой джоб."""
    out = []
    # Мастер rugent просит снять джоб проекта mop — origin джоба говорит, чей
    # он, и это единственная правда (Meta.origin, как в lib.guard).
    why = cluster.refusal("rugent", "delete", origin=MOP, name="pu-mop-1")
    if why is None:
        out.append("a master must NOT reach another project's job")
    elif "mop" not in why:
        out.append(f"the refusal must name the owner: {why!r}")

    if cluster.refusal("rugent", "delete", origin=RUGENT, name="pu-rugent-1") is not None:
        out.append("a master must reach its own job")

    # Завод папета: проект считается из origin, и подсунуть чужой нельзя —
    # иначе мастер rugent заводил бы папетов в проекте mop.
    if cluster.refusal("rugent", "add", origin=MOP) is None:
        out.append("add with another project's origin must be refused")
    if cluster.refusal("rugent", "add", origin=RUGENT) is not None:
        out.append("add with its own origin must be allowed")

    # Смена репозитория — тоже в границах проекта. Иначе мастер rugent
    # переводит своего папета на чужой origin и читает чужой репозиторий
    # ключом пула: креды на шине остаются его, а клон уже не его.
    if cluster.refusal("rugent", "update", origin=RUGENT, name="pu-rugent-1",
                       new_origin=MOP) is None:
        out.append("update onto another project's origin must be refused")
    if cluster.refusal("rugent", "update", origin=RUGENT, name="pu-rugent-1",
                       new_origin=RUGENT) is not None:
        out.append("update within its own project must be allowed")
    if cluster.refusal("admin", "update", origin=RUGENT, name="pu-rugent-1",
                       new_origin=MOP) is not None:
        out.append("the operator may move a puppet between projects")

    # Джоба нет вовсе — это не отказ прав, а отсутствие джоба: разные ответы,
    # иначе опечатка в имени читается как «нет прав».
    if cluster.refusal("rugent", "restart", name="pu-rugent-9") is not None:
        out.append("a missing job is not a permission refusal")

    # Джоб есть, а метки нет: спека старой регистрации, которую никогда не
    # перерегистрировали (puppets.visible). Чей он — из него самого не узнать,
    # поэтому мастеру проекта он не отдаётся; оператору отдаётся, как в ростере.
    if cluster.refusal("rugent", "delete", name="pu-old-1", job_exists=True) is None:
        out.append("an unmarked job must not be reachable by a project master")
    if cluster.refusal("admin", "delete", name="pu-old-1", job_exists=True) is not None:
        out.append("an unmarked job must stay reachable by the operator")
    return out


def check_gone_job():
    """Снятый джоб: читающий глагол отвечает, действующий отказывает.

    HYPOTHESIS (#89): `alloc` отвечал «no job … in the cluster» на любой
    снятый джоб, и `puppets.delete` падал между снятием джоба и сносом тела —
    тело оставалось работать сиротой.
    SOLUTION: `alloc` отдаёт alloc=None; гейт существования остаётся у
    глаголов, которые что-то делают.
    STATUS: FIXED — see #89
    """
    out = []
    gone = {"verb": "alloc", "name": "pu-rugent-9"}
    got = cluster.answer("admin", gone)
    if got.get("error") or "alloc" not in got:
        out.append(f"alloc of a job that is gone must answer, not refuse: {got}")
    elif got["alloc"] is not None:
        out.append(f"alloc of a job that is gone must be None: {got}")

    for verb in ("restart", "stop", "delete", "update"):
        got = cluster.answer("admin", {"verb": verb, "name": "pu-rugent-9"})
        if not got.get("error"):
            out.append(f"{verb} of a job that is gone must refuse: {got}")
    return out


def check_limit():
    """Потолок папетов проекта (#107): add сверх него -- отказ.

    STATUS: FIXED — see #107
    """
    out = []
    if cluster.over_limit("rugent", 2, None) is not None:
        out.append("no limit must never refuse")
    if cluster.over_limit("rugent", 2, 3) is not None:
        out.append("below the limit add must pass")
    why = cluster.over_limit("rugent", 3, 3)
    # Отказ называет проект, счёт и потолок: иначе «не заводится» ищут в
    # Nomad и в слотах узлов.
    if not why or "rugent" not in why or "3" not in why:
        out.append(f"at the limit add must be refused with numbers: {why!r}")
    # 0 -- заморозка: новых не заводить вовсе.
    if cluster.over_limit("rugent", 0, 0) is None:
        out.append("limit 0 must refuse every add")
    return out


def check_project_verbs():
    """HYPOTHESIS (#117): проекты заводил только прогон ansible на
    контроллере, и с машины оператора `mop project add` падал.
    SOLUTION: глаголы проектов у сервиса кластера -- только оператору: мастер
    проекта не заводит чужих проектов и не поднимает себе лимит.
    STATUS: FIXED — see #117"""
    out = []
    for verb in ("projects", "project_add", "project_delete", "project_limit"):
        if verb not in cluster.ADMIN_VERBS:
            out.append(f"{verb} must be an operator's verb")
            continue
        if cluster.refusal("rugent", verb) is None:
            out.append(f"a project's master must not get {verb}")
        if cluster.refusal("admin", verb) is not None:
            out.append(f"the operator must get {verb}")
    try:
        why = cluster.delete_refusal("ghost", [], [])
    except AttributeError:
        return out + ["cluster.delete_refusal is missing"]
    # Нет в реестре -- отказ с именем: опечатка иначе читалась бы снятием.
    if not why or "ghost" not in why:
        out.append(f"deleting an unknown project must be refused by name: {why!r}")
    # Живые папеты -- отказ с их именами: мастер потерял бы шину под ними.
    why = cluster.delete_refusal("rugent", ["git@h:g/rugent.git"], ["pu-rugent-2", "pu-rugent-1"])
    if not why or "pu-rugent-1" not in why:
        out.append(f"deleting a project with puppets must name them: {why!r}")
    if cluster.delete_refusal("rugent", ["git@h:g/rugent.git"], []) is not None:
        out.append("a registered project without puppets must be deletable")
    return out


def check_secret_verbs():
    """HYPOTHESIS (#127): секреты проекта класть было некуда. SOLUTION:
    глаголы сервиса на субъекте проекта -- мастеру своего проекта и
    оператору от имени проекта; у псевдопроекта admin секретов нет.
    STATUS: FIXED — see #127"""
    out = []
    for verb in ("secret_put", "secret_list", "secret_remove"):
        if verb not in cluster.PROJECT_VERBS:
            out.append(f"{verb} must be a project's verb")
            continue
        if cluster.refusal("rugent", verb) is not None:
            out.append(f"a project's master must get {verb}: {cluster.refusal('rugent', verb)}")
        if cluster.refusal("admin", verb) is None:
            out.append(f"{verb} on the admin pseudo-project must be refused: secrets belong to a project")
    return out


def check_verb_table_173():
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
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    from cli import no_network
    out = []
    here = os.path.dirname(os.path.realpath(__file__))
    with open(os.path.join(here, "cluster_verbs_snapshot.json")) as f:
        snap = json.load(f)
    for name, want in snap["sets"].items():
        got = list(getattr(cluster, name))
        if got != want:
            out.append(f"{name} -> {got}, wanted {want}")
    if not isinstance(cluster.VERBS, dict):
        return out + ["cluster.VERBS must be the one table {verb: Verb(...)}"]
    own, foreign = "git@h:g/mop.git", "git@h:g/rugent.git"
    states = {"none": (None, False), "own": (own, True), "foreign": (foreign, True),
              "unlabeled": (None, True)}
    called = []
    undo = no_network()
    keep_table, keep_owner = dict(cluster.VERBS), cluster._owner
    try:
        for v, d in keep_table.items():
            cluster.VERBS[v] = dataclasses.replace(d, fn=(lambda verb: lambda project, req: called.append(verb)
                                              or {"ok": True, "handled": verb})(v))
        for key, want in snap["refusal"].items():
            verb, project, state, new = json.loads(key)
            origin, exists = states[state]
            got = cluster.refusal(project, verb, origin=origin, name="pu-x-1",
                                  job_exists=exists, new_origin=new)
            if got != want:
                out.append(f"refusal {key}: {got!r}, wanted {want!r}")
            cluster._owner = lambda name, o=origin, e=exists: (o, e)
            called.clear()
            got = {"reply": cluster.answer(project, {"verb": verb, "name": "pu-x-1",
                                                     "new_origin": new}),
                   "called": list(called)}
            if got != snap["answer"][key]:
                out.append(f"answer {key}: {got!r}, wanted {snap['answer'][key]!r}")
    finally:
        cluster.VERBS.clear()
        cluster.VERBS.update(keep_table)
        cluster._owner = keep_owner
        undo()
    return out[:20] + ([f"... and {len(out) - 20} more"] if len(out) > 20 else [])


def check_forget_inventory_178():
    """HYPOTHESIS (#178): forget снимает узел с ростера Nomad, а в инвентаре
    контроллера он остаётся, и следующий `mop deploy` молча ставит его снова.
    Предупреждение об этом осталось одной строкой в usage (#159).
    SOLUTION: deploy кладёт список хостов инвентаря файлом рядом с сервисом
    кластера (cluster.INVENTORY_HOSTS), и forget отказывает по нему ДО
    любого вызова Nomad. Файла нет (сервер до этой правки) -- как прежде.
    STATUS: FIXED — see #178"""
    import json
    import tempfile
    from mop import nomad
    out = []
    fn = getattr(cluster, "inventory_refusal", None)
    if fn is None:
        return ["cluster.inventory_refusal(node, hosts) is missing"]
    why = fn("mop-2", ["localhost", "mop-2", "mop-3"])
    if not why or "mop-2" not in why or "mop deploy" not in why:
        out.append(f"a node in the inventory must be refused by name: {why!r}")
    if fn("gone", ["localhost", "mop-2"]) is not None:
        out.append("a node out of the inventory must not be refused by it")
    if fn("mop-2", None) is not None:
        out.append("no host list (a server deployed before #178) must not refuse")

    # Через глагол: отказ -- до Nomad; файла нет -- как прежде. Сводка узла
    # (#196) -- тоже вызов Nomad, и при отказе её не спрашивают.
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    from cli import no_network
    d = tempfile.mkdtemp(prefix="mop-test-forget-")
    path = os.path.join(d, "inventory-hosts.json")
    touched = []
    keep = (getattr(cluster, "INVENTORY_HOSTS", None), nomad.node_summary,
            nomad.node_allocs, nomad.forget_refusal, nomad.node_forget)
    undo = no_network()
    try:
        cluster.INVENTORY_HOSTS = path
        nomad.node_summary = lambda node: touched.append("summary") or {"Name": node}
        nomad.node_allocs = lambda node: touched.append("allocs") or []
        nomad.forget_refusal = lambda node, allocs: None
        nomad.node_forget = lambda node: touched.append("forget")
        with open(path, "w") as f:
            json.dump(["localhost", "mop-2"], f)
        got = cluster.answer("admin", {"verb": "forget", "node": "mop-2"})
        if "mop-2" not in (got.get("error") or "") or touched:
            out.append(f"forget of an inventory host: {got!r}, Nomad touched: {touched}")
        touched.clear()
        got = cluster.answer("admin", {"verb": "forget", "node": "gone"})
        if not got.get("ok") or touched != ["summary", "allocs", "forget"]:
            out.append(f"forget of a host out of the inventory: {got!r}, {touched}")
        os.remove(path)
        touched.clear()
        got = cluster.answer("admin", {"verb": "forget", "node": "mop-2"})
        if not got.get("ok") or touched != ["summary", "allocs", "forget"]:
            out.append(f"forget with no host list must work as before: {got!r}, {touched}")
    finally:
        (cluster.INVENTORY_HOSTS, nomad.node_summary, nomad.node_allocs,
         nomad.forget_refusal, nomad.node_forget) = keep
        undo()
    return out


def check_forget_summary_196():
    """HYPOTHESIS (#196): _forget отдаёт forget_refusal имя узла (строку), а
    та читает сводку -- node['Name'], node.get('Status'); AttributeError, и
    forget с #80 не доходит до purge ни на одном узле.
    SOLUTION: _forget берёт сводку узла (nomad.node_summary) и отдаёт её;
    узла нет -- отказ с именем, без allocs и purge.
    STATUS: FIXED — see #196"""
    from mop import nomad
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    from cli import no_network
    out = []
    touched = []
    closed = {"Name": "mop-2", "ID": "n2", "Status": "ready",
              "SchedulingEligibility": "ineligible"}
    allocs = []
    keep = (nomad.node_summary, nomad.node_allocs, nomad.node_forget)
    undo = no_network()
    try:
        nomad.node_summary = lambda name: dict(closed) if name == "mop-2" else None
        nomad.node_allocs = lambda name: touched.append("allocs") or list(allocs)
        nomad.node_forget = lambda name: touched.append("forget")
        got = cluster.answer("admin", {"verb": "forget", "node": "mop-2"})
        if not got.get("ok") or touched != ["allocs", "forget"]:
            out.append(f"forget of a drained node must reach purge: {got!r}, {touched}")
        touched.clear()
        allocs[:] = [{"JobID": "pu-mop-1", "ClientStatus": "running"}]
        got = cluster.answer("admin", {"verb": "forget", "node": "mop-2"})
        err = got.get("error") or ""
        if "pu-mop-1" not in err or "drain" not in err or "forget" in touched:
            out.append(f"forget of a node with a live alloc must refuse with the job: "
                       f"{got!r}, {touched}")
        touched.clear()
        got = cluster.answer("admin", {"verb": "forget", "node": "ghost"})
        if "ghost" not in (got.get("error") or "") or touched:
            out.append(f"forget of an unknown node must refuse by name, touching "
                       f"nothing: {got!r}, {touched}")
    finally:
        nomad.node_summary, nomad.node_allocs, nomad.node_forget = keep
        undo()
    return out


def check_gates_40():
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
    from mop import bootstrap, bus, nomad, spec
    from mop.domain import Owner
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    from cli import no_network
    out = []
    if not hasattr(cluster, "_clone_of"):
        return ["cluster._clone_of(name) is missing"]
    olga = Owner("olga", int(time.time()) - 60).to_dict()
    work = {"clone": {"cur": "bug/1-x", "def": "master", "dirty": 1, "ahead": 0,
                      "owner": olga}}
    changed, asked = [], []
    answer = {"now": work}
    keep = (cluster._clone_of, cluster._owner, nomad.latest_alloc, nomad.alloc_restart,
            nomad.alloc_stop, nomad.register, nomad.deregister, spec.job_spec,
            bootstrap.store)
    undo = no_network()
    try:
        cluster._clone_of = lambda name: asked.append(name) or answer["now"]
        cluster._owner = lambda name: (MOP, True)
        nomad.latest_alloc = lambda name: {"ID": "a1", "NodeName": "n1"}
        nomad.alloc_restart = lambda alloc: changed.append("restart")
        nomad.alloc_stop = lambda alloc: changed.append("stop")
        nomad.register = lambda job: changed.append("register")
        nomad.deregister = lambda name, purge=True: changed.append("deregister")
        spec.job_spec = lambda *a, **k: {"Job": "x"}
        bootstrap.store = lambda *a, **k: changed.append("store")
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
            if "olga" not in (got.get("error") or "") or changed:
                out.append(f"{label} by another master must be refused naming olga "
                           f"before any Nomad change: {got!r}, {changed}")
            got = ask("mop")
            if not got.get("error") or changed:
                out.append(f"{label} by an anonymous caller must be refused: {got!r}, {changed}")
            got = ask("mop", owner="olga")
            if got.get("error") or not changed:
                out.append(f"{label} by the owner must pass: {got!r}, {changed}")
            got = ask("mop", owner="anton", force=True)
            if got.get("error") or not changed or "olga" not in (got.get("owner_note") or ""):
                out.append(f"{label} with force must pass naming olga: {got!r}, {changed}")
            got = ask("admin", owner="anton")
            if got.get("error") or not changed or asked:
                out.append(f"{label} by the operator must pass without asking: "
                           f"{got!r}, {changed}, asked {asked}")
            # Ничей и без owner -- как до #40.
            answer["now"] = {"clone": dict(work["clone"], owner=None)}
            got = ask("mop")
            if got.get("error") or not changed:
                out.append(f"{label} of nobody's puppet must pass as before: {got!r}")
            answer["now"] = None
            got = ask("mop", owner="anton")
            if got.get("error") or not changed:
                out.append(f"{label} with no allocation must pass: {got!r}")
            answer["now"] = {"error": "node agent n1 is not subscribed"}
            got = ask("mop", owner="anton")
            if "n1" not in (got.get("error") or "") or changed:
                out.append(f"{label} with a silent agent must be refused with the "
                           f"reason: {got!r}, {changed}")
            got = ask("mop", owner="anton", force=True)
            if got.get("error") or not changed:
                out.append(f"{label} with a silent agent and force must pass: {got!r}")
    finally:
        (cluster._clone_of, cluster._owner, nomad.latest_alloc, nomad.alloc_restart,
         nomad.alloc_stop, nomad.register, nomad.deregister, spec.job_spec,
         bootstrap.store) = keep
        undo()

    # Факты -- у агента узла аллокации, через шину, глаголом clone.
    keep = (nomad.latest_alloc, bus.request)
    undo = no_network()
    try:
        calls = []
        nomad.latest_alloc = lambda name: {"ID": "a1", "NodeName": "n1"}
        bus.request = lambda node, verb, **k: calls.append((node, verb, k.get("name"))) \
            or {"clone": None}
        got = cluster._clone_of("pu-mop-1")
        if got != {"clone": None} or calls != [("n1", "clone", "pu-mop-1")]:
            out.append(f"_clone_of must ask the node's agent: {got!r}, {calls}")
        nomad.latest_alloc = lambda name: None
        if cluster._clone_of("pu-mop-1") is not None:
            out.append("_clone_of without an allocation must be None")

        def silent(node, verb, **k):
            raise bus.BusError("node agent n1 is not subscribed")
        nomad.latest_alloc = lambda name: {"ID": "a1", "NodeName": "n1"}
        bus.request = silent
        got = cluster._clone_of("pu-mop-1")
        if "n1" not in ((got or {}).get("error") or ""):
            out.append(f"_clone_of with a silent agent must say so: {got!r}")
    finally:
        nomad.latest_alloc, bus.request = keep
        undo()
    return out


def main():
    failed = []
    for check in (check_subject, check_verbs, check_ownership, check_gone_job,
                  check_limit, check_project_verbs,
                  check_secret_verbs, check_verb_table_173,
                  check_forget_inventory_178, check_forget_summary_196,
                  check_gates_40):
        for line in check():
            failed.append(f"FAIL {check.__name__}: {line}")
    if failed:
        print("\n".join(failed))
    print("cluster: FAILED" if failed else "cluster: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
