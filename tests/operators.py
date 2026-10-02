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
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.server import natsconf, operators  # noqa: E402
from mop.common import busnames, service  # noqa: E402


def check_parse(c):
    """Разбор настройки: кто есть, в какой роли и на какие проекты (#106)."""
    got = operators.parse("anton:admin; ivan:user:cloudpub,rugent ;  ")
    want = {"anton": {"role": "admin", "projects": ["*"]},
            "ivan": {"role": "user", "projects": ["cloudpub", "rugent"]}}
    c.expect("parse", got, want)

    # user на весь пул -- не admin: «все проекты» и «машинные глаголы» были
    # одним флагом `*`, и дать первое без второго было нельзя.
    got = operators.parse("ivan:user:*")
    c.expect("user:*", got, {"ivan": {"role": "user", "projects": ["*"]}})

    # Переход: прежняя запись без роли читается так, как работала до #106 --
    # `*` давал весь mop.>, то есть admin; список проектов -- user.
    got = operators.parse("anton:*; ivan:mop,rugent")
    want = {"anton": {"role": "admin", "projects": ["*"]},
            "ivan": {"role": "user", "projects": ["mop", "rugent"]}}
    c.expect("legacy", got, want)

    c.check("an empty setting must give no operators, not an error",
            not (operators.parse("") != {}))

    # Права не подразумеваются никогда. admin с проектами -- отказ, а не
    # молчаливое сужение или расширение: запись говорит одно, права другое.
    for bad in ("anton", "anton:", ":mop", "ivan:user", "ivan:user:",
                "anton:admin:mop", "anton:owner:mop", "anton:user:mop:x"):
        try:
            operators.parse(bad)
            refused = False
        except ValueError:
            refused = True
        c.check(f"{bad!r} must be refused: rights are never implied", refused)


def check_reserved(c):
    """Имя человека не должно совпасть с ролевым: это тихая подмена роли."""
    # service (#104) -- машинный пользователь сервисов сервера: человек с
    # этим именем получил бы права сервисов под видом оператора.
    for bad in ("admin", "service", "master-mop", "puppet-mop", "node-mate"):
        try:
            operators.parse(f"{bad}:user:mop")
            refused = False
        except ValueError:
            refused = True
        c.check(f"{bad!r} must be refused as an operator name", refused)
    # Обычное человеческое имя проходит.
    c.check("an ordinary name must be accepted",
            not (operators.parse("anton:user:mop") != {"anton": {"role": "user", "projects": ["mop"]}}))


def check_permissions(c):
    """Права пользователя по роли: свои проекты и инбоксы, и ничего сверх.

    allow/deny -- подписка: явным списком того, на что подписывается клиент
    (#212) -- свои инбоксы мастеров и опрос who (#213), события, _INBOX;
    публикация -- явным
    списком (publish/publish_deny), логин в rpc -- свой (#207;
    tests/natsconf.py проверяет обе правилами файла)."""
    def sub(got):
        return {"allow": got["allow"], "deny": got["deny"]}
    got = operators.permissions({"role": "user", "projects": ["rugent", "mop"]}, "ivan")
    want = {"allow": ["mop.mop.master.ivan.>", "mop.mop.master.all.inbox", "mop.mop.events",
                      "mop.rugent.master.ivan.>", "mop.rugent.master.all.inbox",
                      "mop.rugent.events", "_INBOX.>"], "deny": []}
    c.check("user", not (sub(got) != want), f"{got}, wanted {want}")
    want_pub = [f"mop.{p}.{t}" for p in ("mop", "rugent") for t in (
        "node.*.rpc.ivan", "cluster.rpc.ivan", "node.*.rpc", "cluster.rpc", "node.*.msg",
        "all.msg", "master.>", "events", "server.rpc")] + ["mopjoin.ivan.rpc", "_INBOX.>"]
    c.check("user publishes", not (got["publish"] != want_pub or got["publish_deny"] != []),
            f"{got['publish']}, {got['publish_deny']}")
    # admin -- весь mop.>: все проекты плюс машинные глаголы в mop.admin.*.
    got = operators.permissions({"role": "admin", "projects": ["*"]}, "anton")
    c.check("admin", not (sub(got) != {"allow": ["mop.*.master.anton.>", "mop.*.master.all.inbox",
                                                 "mop.*.events", "_INBOX.>"], "deny": []}), got)
    c.check("admin publishes the builder and no deny",
            not ("mop.admin.build.rpc" not in got["publish"] or got["publish_deny"]), got)
    # user:* -- все проекты, но не машины: mop.admin.> закрыт явно, иначе
    # mop.> отдал бы и disk, и drain узлов.
    got = operators.permissions({"role": "user", "projects": ["*"]}, "olga")
    c.check("user:*",
            not (sub(got) != {"allow": ["mop.*.master.olga.>", "mop.*.master.all.inbox",
                                        "mop.*.events", "_INBOX.>"], "deny": ["mop.admin.>"]}
                 or got["publish_deny"] != ["mop.admin.>"]
                 or "mop.admin.build.rpc" in got["publish"]), got)
    # _INBOX обязателен: без него request-reply молча не работает.
    got = operators.permissions({"role": "user", "projects": ["mop"]}, "ivan")
    c.check("_INBOX.> is mandatory or request-reply silently fails",
            not ("_INBOX.>" not in got["allow"] or "_INBOX.>" not in got["publish"]))
    # STATUS: FIXED — see #106


# HYPOTHESIS: обычный server.rpc открыт узлам, а проектные маски user:*
# позволяют публиковать от чужого логина. Ключу нужен отдельный адрес.
# SOLUTION: один субъект вне проектной маски, публикуемый человеком только
# под своим токеном; сервис слушает, машины и паппеты не публикуют.
# RESULT: у людей личный субъект, проектные маски и машины его не открывают.
# STATUS: FIXED — see #404
def check_join_subject_404(c):
    address = getattr(busnames, "join_config", None)
    if not c.check("#404 private join subject exists", address is not None):
        return
    for role, projects, login in (("admin", ["*"], "anton"),
                                  ("user", ["mop"], "ivan"),
                                  ("user", ["*"], "olga")):
        pub = operators.permissions({"role": role, "projects": projects}, login)["publish"]
        c.check(f"#404 {role} can publish only their joined-config subject",
                address(login) in pub and address("other") not in pub, pub)
        c.check(f"#404 {role} subject is outside project wildcard",
                not address(login).startswith("mop."), address(login))
        c.expect("#404 caller is taken from the subject", busnames.caller(address(login)),
                 login)
        c.expect("#404 joined-config subject has no project", service.project_from_subject(address(login)),
                 None)
    c.check("#404 service subscribes to the private address",
            address(busnames.ANY) in busnames.service_subscriptions()
            and address(busnames.ANY) in natsconf.SERVICE_PERMISSIONS)
    c.check("#404 node cannot publish joined-config request",
            address("ivan") not in natsconf.node_permissions("n1")["publish"])
    c.check("#404 puppet cannot publish joined-config request",
            address("ivan") not in natsconf.puppet_permissions("mop")["publish"])


def main():
    c = Checks()
    for fn in (check_parse, check_reserved, check_permissions, check_join_subject_404):
        fn(c)
    return c.report("operators")


if __name__ == "__main__":
    sys.exit(main())
