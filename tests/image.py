#!/usr/bin/env python3
"""Проверка сборки образа шарда без пула: python3 tests/image.py

Чистая часть сборки — что уезжает плейбуку в --extra-vars из манифеста.
Ошибка тихая и уже случалась (#26): None-половина, переданная как null,
не подменяется `default('')` в условиях плейбука, и len(None) ронял сборку
шарда, у которого есть только задачи. Поэтому половины без содержимого не
передаются вовсе, и это свойство держит эта проверка (#49).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import image  # noqa: E402

GOT = {"shard": "proj", "asks": {"MOP_MEM_MB": "2048"}, "alien": [],
       "node_tasks": None, "ws_vars": None, "ws_tasks": "/tmp/x/ws-tasks.yml"}


def main():
    failed = 0
    extra = image.extra_vars("git@h:g/proj.git", GOT)
    want = {"mop_shard": "proj", "mop_origin": "git@h:g/proj.git",
            "mop_shard_asks": {"MOP_MEM_MB": "2048"},
            "mop_shard_tasks": "/tmp/x/ws-tasks.yml"}
    if extra != want:
        failed += 1
        print(f"FAIL extra_vars: {extra} != {want}")
    if "mop_shard_vars" in extra:
        failed += 1
        print("FAIL extra_vars: a None half must be absent, not null")
    print("image: FAILED" if failed else "image: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
