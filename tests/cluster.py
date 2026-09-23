#!/usr/bin/env python3
"""Права сервиса кластера без пула: python3 tests/cluster.py

Сервис кластера — единственное, что говорит с Nomad (#80). Всё, что раньше
мастер делал сам с management-токеном, он теперь просит глаголом, а сервис
решает, можно ли. Решение и проверяется здесь: оно чистое, и ошибка в нём —
это не отказ, а тихо выполненная чужая команда.

Главное свойство: проект берётся из СУБЪЕКТА, а не из тела запроса. Субъект
проверен правами NATS (`master-<проект>` пишет только в `mop.<проект>.>`),
поле в теле подделывает кто угодно.

HYPOTHESIS (#80): мастер ходит в Nomad напрямую management-токеном, и шард в
имени джоба никто не сверяет — мастер проекта A снимает джоб проекта B.
SOLUTION: глаголы на шине, сервис сверяет проект из субъекта с origin джоба,
как агент сверяет проект с origin клона.
STATUS: FIXED — see #80
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import cluster  # noqa: E402

RUGENT = "git@git.ermak.dev:rugent/rugent.git"
MOP = "git@git.ermak.dev:ermak/mop.git"


def check_subject():
    """Проект — первый токен субъекта, и никогда не поле запроса."""
    out = []
    for subj, want in [("mop.rugent.cluster.rpc", "rugent"),
                       ("mop.admin.cluster.rpc", "admin"),
                       ("mop.mop.cluster.rpc", "mop")]:
        got = cluster.project_of(subj)
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


def main():
    failed = []
    for check in (check_subject, check_verbs, check_ownership):
        for line in check():
            failed.append(f"FAIL {check.__name__}: {line}")
    if failed:
        print("\n".join(failed))
    print("cluster: FAILED" if failed else "cluster: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
