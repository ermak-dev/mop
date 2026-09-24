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
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import image  # noqa: E402

# В образ едет только sandbox (#61): bootstrap играется при старте песочницы
# сервером, и в сборке ему делать нечего.
GOT = {"project": "proj", "asks": {"MOP_MEM_MB": "2048"}, "alien": [], "legacy": [],
       "sandbox_vars": None, "sandbox_tasks": "/tmp/x/sandbox-tasks.yml",
       "bootstrap_vars": "/tmp/x/bootstrap-vars.yml",
       "bootstrap_tasks": "/tmp/x/bootstrap-tasks.yml"}


def main():
    failed = 0
    extra = image.extra_vars("git@h:g/proj.git", GOT)
    want = {"mop_project": "proj", "mop_origin": "git@h:g/proj.git",
            "mop_project_asks": {"MOP_MEM_MB": "2048"},
            "mop_project_tasks": "/tmp/x/sandbox-tasks.yml"}
    if extra != want:
        failed += 1
        print(f"FAIL extra_vars: {extra} != {want}")
    if "mop_project_vars" in extra:
        failed += 1
        print("FAIL extra_vars: a None half must be absent, not null")
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
            failed += 1
            print("FAIL plan_clear: a busy puppet on a container node must refuse")
        except RuntimeError as e:
            for who in ("pu-proj-2", "pu-proj-4"):
                if who not in str(e):
                    failed += 1
                    print(f"FAIL plan_clear: the refusal must name {who}: {e}")
            if "pu-proj-3" in str(e) or "pu-proj-1" in str(e):
                failed += 1
                print(f"FAIL plan_clear: the refusal names a puppet that does not block: {e}")
        got = image.plan_clear(rows, force=True)
        if got != ["pu-proj-1", "pu-proj-2", "pu-proj-4"]:
            failed += 1
            print(f"FAIL plan_clear(force): {got}")
        got = image.plan_clear(rows[:1])
        if got != ["pu-proj-1"]:
            failed += 1
            print(f"FAIL plan_clear(free only): {got}")
    except AttributeError:
        failed += 1
        print("FAIL image.plan_clear is missing")

    print("image: FAILED" if failed else "image: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
