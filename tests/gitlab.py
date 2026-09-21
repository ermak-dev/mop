#!/usr/bin/env python3
"""Чистая логика трекера без GitLab: python3 tests/gitlab.py

Переходы меток и имена веток — единственное в `mop/gitlab.py`, что можно
проверить без сети, и единственное, где ошибка молчит: лишняя метка одной
группы не отказ, GitLab оставит одну из двух и не скажет какую, а задача
выпадет из выборки, которой её ищут.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import gitlab  # noqa: E402

ORIGINS = [
    ("git@git.example.dev:group/proj.git", ("git.example.dev", "group/proj")),
    ("ssh://git@git.example.dev:2222/group/sub/proj.git",
     ("git.example.dev", "group/sub/proj")),
    ("https://git.example.dev/group/proj.git", ("git.example.dev", "group/proj")),
    ("", ("", "")),
]

BRANCHES = [
    (["bug", "sev::high"], 12, None, "bug/12"),
    (["bug"], 12, "Папет залипает на диалоге", "bug/12"),
    (["bug"], 12, "clone holds work", "bug/12-clone-holds-work"),
    (["feature"], 7, "driver seam", "feat/7-driver-seam"),
    (["type::docs"], 3, "BUS", "docs/3-bus"),
    # Дефект перевешивает тип: задача не должна нести оба, но если несёт —
    # правда о работе в дефекте.
    (["bug", "feature"], 9, None, "bug/9"),
    ([], 1, None, "feat/1"),
]

STATUS = [
    (["status::live", "sev::low"], "wip", ["sev::low", "status::wip"]),
    (["status::wip"], "fixed", ["status::fixed"]),
    # Идемпотентность: повторный переход не кладёт вторую метку группы.
    (["status::wip"], "wip", ["status::wip"]),
    ([], "live", ["status::live"]),
]


# Эпик — не сущность GitLab (она в платной редакции), а маркер в теле задачи:
# первая строка «**Эпик:** #N». Отсюда два требования, и оба молчаливые:
# маркер не должен задваиваться при повторной правке, а смена родителя не
# должна оставлять рядом старую строку — иначе у задачи два родителя и оба
# «настоящие».
EPICS = [
    ("текст", 5, "**Эпик:** #5\n\nтекст"),
    ("**Эпик:** #5\n\nтекст", 5, "**Эпик:** #5\n\nтекст"),
    ("**Эпик:** #4\n\nтекст", 5, "**Эпик:** #5\n\nтекст"),
    ("", 7, "**Эпик:** #7"),
]

EPIC_OF = [
    ("**Эпик:** #5\n\nтекст", 5),
    ("текст\n\n**Эпик:** #5", None),   # только первой строкой: ссылка в теле — не родство
    ("текст", None),
    ("", None),
]


def main():
    bad = cases = 0
    for url, want in ORIGINS:
        cases += 1
        if gitlab._parse_origin(url) != want:
            bad += 1
            print(f"FAILED  origin {url!r} -> {gitlab._parse_origin(url)!r}, wanted {want!r}")

    for labels, iid, slug, want in BRANCHES:
        cases += 1
        got = gitlab.branch_name(labels, iid, slug)
        if got != want:
            bad += 1
            print(f"FAILED  branch {labels} #{iid} {slug!r} -> {got!r}, wanted {want!r}")

    for labels, status, want in STATUS:
        cases += 1
        got = gitlab.with_status(labels, status)
        if sorted(got) != sorted(want):
            bad += 1
            print(f"FAILED  with_status({labels}, {status!r}) -> {got!r}, wanted {want!r}")

    cases += 1
    try:
        gitlab.with_status([], "partial")
        bad += 1
        print("FAILED  an unknown status must refuse, not pass through")
    except RuntimeError:
        pass

    for name in ("status::nope", "nope::live"):
        cases += 1
        try:
            gitlab.check_label(name)
            bad += 1
            print(f"FAILED  check_label({name!r}) must refuse")
        except RuntimeError:
            pass

    for body, iid, want in EPICS:
        cases += 1
        got = gitlab.with_epic(body, iid)
        if got != want:
            bad += 1
            print(f"FAILED  with_epic({body!r}, {iid}) -> {got!r}, wanted {want!r}")

    for body, want in EPIC_OF:
        cases += 1
        got = gitlab.epic_of(body)
        if got != want:
            bad += 1
            print(f"FAILED  epic_of({body!r}) -> {got!r}, wanted {want!r}")

    cases += 1
    if gitlab.check_label("component::bus") != "component::bus":
        bad += 1
        print("FAILED  a label from the vocabulary must pass")

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
