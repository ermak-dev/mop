#!/usr/bin/env python3
"""Операторы как пользователи NATS: python3 tests/operators.py

Оператор представлялся шине ролью — `master-<проект>`, с паролем, общим на
всех операторов проекта. Отозвать доступ одному человеку значило сменить
пароль всем, а привезти этот пароль — скопировать каталог по ssh (#84).

Теперь оператор — пользователь с именем человека и правами на свои проекты.
Разбор списка операторов и есть то место, где ошибка стоит дорого: лишнее имя
в правах — это доступ, которого никто не давал, а совпавшее с ролевым — тихая
подмена роли.

HYPOTHESIS (#84): пароль проекта общий на всех, и отзыв доступа одному
человеку невозможен без смены пароля остальным.
SOLUTION: пользователь на человека, список в MOP_OPERATORS, `mop join`
спрашивает логин и пароль вместо копирования каталога.
STATUS: FIXED — see #84
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import operators  # noqa: E402


def check_parse():
    """Разбор настройки: кто есть, в какой роли и на какие проекты (#106)."""
    out = []
    got = operators.parse("anton:admin; ivan:user:cloudpub,rugent ;  ")
    want = {"anton": {"role": "admin", "projects": ["*"]},
            "ivan": {"role": "user", "projects": ["cloudpub", "rugent"]}}
    if got != want:
        out.append(f"parse -> {got}, wanted {want}")

    # user на весь пул -- не admin: «все проекты» и «машинные глаголы» были
    # одним флагом `*`, и дать первое без второго было нельзя.
    got = operators.parse("ivan:user:*")
    if got != {"ivan": {"role": "user", "projects": ["*"]}}:
        out.append(f"user:* -> {got}")

    # Переход: прежняя запись без роли читается так, как работала до #106 --
    # `*` давал весь mop.>, то есть admin; список проектов -- user.
    got = operators.parse("anton:*; ivan:mop,rugent")
    want = {"anton": {"role": "admin", "projects": ["*"]},
            "ivan": {"role": "user", "projects": ["mop", "rugent"]}}
    if got != want:
        out.append(f"legacy -> {got}, wanted {want}")

    if operators.parse("") != {}:
        out.append("an empty setting must give no operators, not an error")

    # Права не подразумеваются никогда. admin с проектами -- отказ, а не
    # молчаливое сужение или расширение: запись говорит одно, права другое.
    for bad in ("anton", "anton:", ":mop", "ivan:user", "ivan:user:",
                "anton:admin:mop", "anton:owner:mop", "anton:user:mop:x"):
        try:
            operators.parse(bad)
        except ValueError:
            continue
        out.append(f"{bad!r} must be refused: rights are never implied")
    return out


def check_reserved():
    """Имя человека не должно совпасть с ролевым: это тихая подмена роли."""
    out = []
    # service (#104) -- машинный пользователь сервисов сервера: человек с
    # этим именем получил бы права сервисов под видом оператора.
    for bad in ("admin", "service", "master-mop", "puppet-mop", "node-mate"):
        try:
            operators.parse(f"{bad}:user:mop")
        except ValueError:
            continue
        out.append(f"{bad!r} must be refused as an operator name")
    # Обычное человеческое имя проходит.
    if operators.parse("anton:user:mop") != {"anton": {"role": "user", "projects": ["mop"]}}:
        out.append("an ordinary name must be accepted")
    return out


def check_permissions():
    """Права пользователя по роли: свои проекты и инбоксы, и ничего сверх.

    allow/deny -- подписка, она та же, что до #207; публикация -- явным
    списком (publish/publish_deny), логин в rpc -- свой (tests/natsconf.py
    проверяет её правилами файла)."""
    out = []

    def sub(got):
        return {"allow": got["allow"], "deny": got["deny"]}
    got = operators.permissions({"role": "user", "projects": ["rugent", "mop"]}, "ivan")
    want = {"allow": ["mop.mop.>", "mop.rugent.>", "_INBOX.>"], "deny": []}
    if sub(got) != want:
        out.append(f"user -> {got}, wanted {want}")
    want_pub = [f"mop.{p}.{t}" for p in ("mop", "rugent") for t in (
        "node.*.rpc.ivan", "cluster.rpc.ivan", "node.*.rpc", "cluster.rpc", "node.*.msg",
        "all.msg", "master.>", "events", "server.rpc")] + ["_INBOX.>"]
    if got["publish"] != want_pub or got["publish_deny"] != []:
        out.append(f"user publishes -> {got['publish']}, {got['publish_deny']}")
    # admin -- весь mop.>: все проекты плюс машинные глаголы в mop.admin.*.
    got = operators.permissions({"role": "admin", "projects": ["*"]}, "anton")
    if sub(got) != {"allow": ["mop.>", "_INBOX.>"], "deny": []}:
        out.append(f"admin -> {got}")
    if "mop.admin.build.rpc" not in got["publish"] or got["publish_deny"]:
        out.append(f"admin publishes the builder and no deny: {got}")
    # user:* -- все проекты, но не машины: mop.admin.> закрыт явно, иначе
    # mop.> отдал бы и disk, и drain узлов.
    got = operators.permissions({"role": "user", "projects": ["*"]}, "olga")
    if sub(got) != {"allow": ["mop.>", "_INBOX.>"], "deny": ["mop.admin.>"]} \
            or got["publish_deny"] != ["mop.admin.>"] or "mop.admin.build.rpc" in got["publish"]:
        out.append(f"user:* -> {got}")
    # _INBOX обязателен: без него request-reply молча не работает.
    got = operators.permissions({"role": "user", "projects": ["mop"]}, "ivan")
    if "_INBOX.>" not in got["allow"] or "_INBOX.>" not in got["publish"]:
        out.append("_INBOX.> is mandatory or request-reply silently fails")
    return out
    # STATUS: FIXED — see #106


def main():
    failed = []
    for check in (check_parse, check_reserved, check_permissions):
        for line in check():
            failed.append(f"FAIL {check.__name__}: {line}")
    if failed:
        print("\n".join(failed))
    print("operators: FAILED" if failed else "operators: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
