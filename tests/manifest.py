#!/usr/bin/env python3
"""Проверка дороги за манифестом без пула: python3 tests/manifest.py

Сборка образа читает `.mop` из зеркала origin, и ветка зеркала — единственное,
что здесь решается без сети (#46). Ошибка тихая: detached HEAD рабочей копии
git называет словом `HEAD`, и ref с таким именем в зеркале — дефолтная ветка,
то есть сборка молча собрала бы не то, что просили.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import manifest  # noqa: E402

# (ветка, ожидаемый ref или ValueError)
# HYPOTHESIS: до #46 fetch читал HEAD зеркала всегда, ref_of не существовало.
# SOLUTION: ref_of(branch): None -> HEAD, имя -> refs/heads/имя, detached -> отказ.
# STATUS: FIXED — see #46
CASES = [
    (None, "HEAD"),
    ("master", "refs/heads/master"),
    ("feat/46-build-branch", "refs/heads/feat/46-build-branch"),
    ("HEAD", ValueError),
    ("", ValueError),
]


def main():
    failed = 0
    for branch, want in CASES:
        try:
            got = manifest.ref_of(branch)
        except ValueError:
            got = ValueError
        except AttributeError as e:
            got = e
        if got != want:
            failed += 1
            print(f"FAIL ref_of({branch!r}): {got!r} != {want!r}")
    print("manifest: FAILED" if failed else "manifest: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
