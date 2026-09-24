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
            cluster.VERBS[v] = d._replace(fn=(lambda verb: lambda project, req: called.append(verb)
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


def main():
    failed = []
    for check in (check_subject, check_verbs, check_ownership, check_gone_job,
                  check_limit, check_project_verbs,
                  check_secret_verbs, check_verb_table_173):
        for line in check():
            failed.append(f"FAIL {check.__name__}: {line}")
    if failed:
        print("\n".join(failed))
    print("cluster: FAILED" if failed else "cluster: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
