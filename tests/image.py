#!/usr/bin/env python3
"""Проверка сборки образа проекта без пула: python3 tests/image.py

Чистая часть сборки — что уезжает плейбуку в --extra-vars из манифеста.
Ошибка тихая и уже случалась (#26): None-половина, переданная как null,
не подменяется `default('')` в условиях плейбука, и len(None) ронял сборку
проекта, у которого есть только задачи. Поэтому половины без содержимого не
передаются вовсе, и это свойство держит эта проверка (#49).
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.server import image  # noqa: E402

# В образ едет только sandbox (#61): bootstrap играется при старте песочницы
# сервером, и в сборке ему делать нечего.
GOT = {"project": "proj", "asks": {"MOP_MEM_MB": "2048"}, "alien": [], "legacy": [],
       "sandbox_vars": None, "sandbox_tasks": "/tmp/x/sandbox-tasks.yml",
       "bootstrap_vars": "/tmp/x/bootstrap-vars.yml",
       "bootstrap_tasks": "/tmp/x/bootstrap-tasks.yml"}


def main():
    c = Checks()
    extra = image.extra_vars("git@h:g/proj.git", GOT)
    want = {"mop_project": "proj", "mop_origin": "git@h:g/proj.git",
            "mop_project_asks": {"MOP_MEM_MB": "2048"},
            "mop_project_tasks": "/tmp/x/sandbox-tasks.yml"}
    c.expect("extra_vars", extra, want)
    c.check("extra_vars: a None half must be absent, not null", "mop_project_vars" not in extra)
    # Пересборка — операция над ПРОЕКТОМ (#60): тела проекта на контейнерных
    # узлах сносятся до сборки (у шаблона с живым клоном не отнять место), и
    # занятый папет — отказ, если не сказано --force. Тела на узлах, где тело
    # равно узлу, сборка не касается вовсе.
    # HYPOTHESIS: plan_clear нет, сборка сносит образ, а папеты остаются жить
    # на старом. SOLUTION: чистый план из строк ростера. STATUS: FIXED — see #60
    rows = [
        {"name": "pu-proj-1", "node": "hyper", "container": True, "kind": "free"},
        {"name": "pu-proj-2", "node": "hyper", "container": True, "kind": "busy"},
        {"name": "pu-proj-3", "node": "mate", "container": False, "kind": "busy"},
        {"name": "pu-proj-4", "node": "hyper", "container": True, "kind": "silent"},
    ]
    try:
        try:
            image.plan_clear(rows)
            c.fail("plan_clear: a busy puppet on a container node must refuse")
        except RuntimeError as e:
            for who in ("pu-proj-2", "pu-proj-4"):
                c.check(f"plan_clear: the refusal must name {who}", who in str(e), e)
            c.check("plan_clear: the refusal names a puppet that does not block",
                    not ("pu-proj-3" in str(e) or "pu-proj-1" in str(e)), e)
        c.expect("plan_clear(force)", image.plan_clear(rows, force=True),
                 ["pu-proj-1", "pu-proj-2", "pu-proj-4"])
        c.expect("plan_clear(free only)", image.plan_clear(rows[:1]), ["pu-proj-1"])
    except AttributeError:
        c.fail("image.plan_clear is missing")
    # Сборка на одном узле (#280): на rumop образ на agent2 запечатался голым
    # (#278), а на agent1 папеты проекта заняты работой. Пересборка всего
    # проекта отбивалась бы по занятым (или --force убил бы их работу), хотя
    # образ agent1 цел. С названным узлом считаются только его тела: чужой
    # занятый папет не блокирует, снимаются только свои, плейбук получает
    # mop_node и играет только там.
    # HYPOTHESIS: plan_clear и bake не знают узла -- сборка всегда над всеми
    # контейнерными узлами. SOLUTION: node= у plan_clear/clear/bake/announce/
    # build, mop_node в extra-vars плейбука, end_host на чужих узлах.
    # STATUS: FIXED — see #280
    both = [
        {"name": "pu-proj-1", "node": "agent1", "container": True, "kind": "busy"},
        {"name": "pu-proj-2", "node": "agent2", "container": True, "kind": "failing"},
        {"name": "pu-proj-3", "node": "agent2", "container": True, "kind": "free"},
    ]
    try:
        try:
            c.expect("plan_clear(node): a busy puppet elsewhere does not block",
                     image.plan_clear(both, node="agent2", force=True),
                     ["pu-proj-2", "pu-proj-3"])
        except RuntimeError as e:
            c.fail(f"plan_clear(node): refused by another node's puppet: {e}")
        try:
            image.plan_clear(both, node="agent2")
            c.fail("plan_clear(node): a not-free puppet on the node must still refuse")
        except RuntimeError as e:
            c.check("plan_clear(node): the refusal names only the node's puppets",
                    "pu-proj-2" in str(e) and "pu-proj-1" not in str(e), e)
        c.expect("plan_clear(node): only the node's puppets are cleared",
                 image.plan_clear(both, node="agent1", force=True), ["pu-proj-1"])
        c.expect("plan_clear(no node): everyone, as before",
                 image.plan_clear(both, force=True),
                 ["pu-proj-1", "pu-proj-2", "pu-proj-3"])
    except TypeError as e:
        c.fail(f"plan_clear takes no node: {e}")
    try:
        c.check("play_extra: no node -> no mop_node",
                "mop_node" not in image.play_extra("git@h:g/proj.git", GOT, False, None))
        got = image.play_extra("git@h:g/proj.git", GOT, True, "agent2")
        c.expect("play_extra: node -> mop_node", got.get("mop_node"), "agent2")
        c.expect("play_extra: fresh still travels", got.get("mop_fresh"), True)
        c.expect("play_extra: the manifest half is extra_vars",
                 {k: v for k, v in got.items() if k not in ("mop_node", "mop_fresh")},
                 image.extra_vars("git@h:g/proj.git", GOT))
    except AttributeError:
        c.fail("image.play_extra is missing")
    return c.report("image")


if __name__ == "__main__":
    sys.exit(main())
