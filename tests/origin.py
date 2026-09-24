#!/usr/bin/env python3
"""Разбор origin'а без пула: python3 tests/origin.py

Правило проекта (basename без .git) и разбор origin'а на хост и путь были
написаны по нескольку раз с разными правилами (#154): gitlab._ORIGIN,
projects.git_hosts, puppets.looks_like_origin/project_ids, puppets.project_of
и его копия в агенте. Здесь -- один разборщик, driver.parse_origin, и таблица
ответов каждого прежнего потребителя: рефакторинг обязан отвечать им то же.

Это не фреймворк и не прогон всего проекта: остальное по-прежнему добывается
на живом пуле.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, ROOT)

from mop import driver, gitlab, projects, puppets  # noqa: E402

# HYPOTHESIS: три разборщика хоста с разными правилами дают разные ответы на
# одном origin'е. Характеризация до правки показала: на формах, что бывают
# на деле, они уже совпадают -- расходятся только на мусоре (ниже, CHANGED).
# SOLUTION: driver.parse_origin, все потребители через него; project_of --
# рядом с project_of_name, агент берёт его оттуда.
# RESULT: таблица прежних ответов держится целиком, кроме строк CHANGED.
# STATUS: FIXED — see #154

# (origin, gitlab (хост, путь), git_hosts без умолчания, looks_like, project_of)
# Снято с кода до правки (6aeb0c0) и обязано остаться.
SAME = [
    ("git@git.ermak.dev:ermak/mop.git", ("git.ermak.dev", "ermak/mop"),
     ["git.ermak.dev"], True, "mop"),
    ("git@h:g/sub/p.git", ("h", "g/sub/p"), ["h"], True, "p"),
    ("git@h:g/p", ("h", "g/p"), ["h"], True, "p"),
    ("h:g/p", ("h", "g/p"), ["h"], True, "p"),
    ("git@h:/abs/p.git", ("h", "/abs/p"), ["h"], True, "p"),
    ("ssh://git@h:2222/g/sub/p.git", ("h", "g/sub/p"), ["[h]:2222"], True, "p"),
    ("ssh://git@h/g/p.git", ("h", "g/p"), ["h"], True, "p"),
    ("ssh://git@h:22/g/p", ("h", "g/p"), ["h"], True, "p"),
    ("https://h/g/sub/p.git", ("h", "g/sub/p"), [], True, "p"),
    ("https://h:8443/g/p", ("h", "g/p"), [], True, "p"),
    ("https://u:tok@h/g/p.git", ("h", "g/p"), [], True, "p"),
    ("git://h/g/p.git", ("h", "g/p"), [], True, "p"),
    ("/srv/git/p.git", ("", ""), [], True, "p"),
    # Хвостовой / и scp без группы: до #165 project_of давал здесь пусто и
    # «git@h:p» (имя папета pu-git@h:p-1, valid_name его и не пускал).
    ("ssh://h/g/p/", ("h", "g/p"), ["h"], True, "p"),
    ("http://h/g/p.git/", ("h", "g/p"), [], True, "p"),
    ("https://h/g/p/", ("h", "g/p"), [], True, "p"),
    ("git@h:p.git", ("h", "p"), ["h"], True, "p"),
    # git+ssh:// -- тоже ssh, ключ хоста нужен (#166; до него -- пусто).
    ("git+ssh://git@h/g/p.git", ("h", "g/p"), ["h"], True, "p"),
    ("mop", ("", ""), [], False, "mop"),
    ("", ("", ""), [], False, ""),
    ("  ", ("", ""), [], False, "  "),
    # Мусор: до #165 -- «:x» как есть, теперь путь разбора.
    (":x", ("", ""), [], True, "x"),
    ("x:", ("", ""), ["x"], True, "x:"),
]

# Мусор, на котором прежние разборщики отвечали мусором: на деле таких
# origin'ов нет (git их не клонирует или это не GitLab). (origin, было, стало)
CHANGED = [
    ("file:///srv/git/p.git", "gitlab", ("file", "///srv/git/p"), ("", "")),
    ("./rel/p", "gitlab", (".", "rel/p"), ("", "")),
    ("srv/git/p", "gitlab", ("srv", "git/p"), ("", "")),
    ("HTTPS://h/g/p", "gitlab", ("HTTPS", "//h/g/p"), ("h", "g/p")),
    ("ssh://h/", "gitlab", ("ssh", "//h"), ("", "")),
    # Без пути: порт уезжал в путь проекта.
    ("ssh://git@h:2222", "gitlab", ("h", "2222"), ("", "")),
    # scp-форма порта не знает: «2222» -- первый сегмент пути, не порт.
    ("git@h:2222/g/p.git", "gitlab", ("h", "g/p"), ("h", "2222/g/p")),
    # Пробелы по краям: git их отрезает, looks_like и git_hosts резали, а
    # gitlab тащил в путь.
    (" git@h:g/p.git ", "gitlab", ("h", "g/p.git "), ("h", "g/p")),
    # Слеш до двоеточия -- локальный путь (так решает сам git), не scp-хост.
    ("a/b:c", "git_hosts", ["a/b"], []),
    ("./a:b", "git_hosts", ["./a"], []),
]

# driver.parse_origin: (схема, пользователь, хост, порт, путь без .git).
PARSE = [
    ("ssh://git@h:2222/g/sub/p.git", ("ssh", "git", "h", "2222", "g/sub/p")),
    ("ssh://h/g/p", ("ssh", None, "h", None, "g/p")),
    ("git@h:g/sub/p.git", ("ssh", "git", "h", None, "g/sub/p")),
    ("h:g/p", ("ssh", None, "h", None, "g/p")),
    ("https://h/g/p.git", ("https", None, "h", None, "g/p")),
    ("https://u:tok@h:8443/g/p/", ("https", "u:tok", "h", "8443", "g/p")),
    ("HTTPS://h/g/p", ("https", None, "h", None, "g/p")),
    ("file:///srv/git/p.git", ("file", None, "", None, "/srv/git/p")),
    ("/srv/git/p.git", ("file", None, "", None, "/srv/git/p")),
    ("mop", None),
    ("", None),
    (None, None),
]


def consumers(url):
    return {"gitlab": gitlab._parse_origin(url),
            "git_hosts": projects.git_hosts([url], "D")[1:],
            "looks_like": puppets.looks_like_origin(url),
            "project_of": puppets.project_of(url)}


def main():
    bad = cases = 0

    def fail(msg):
        nonlocal bad
        bad += 1
        print(f"FAILED  {msg}")

    for url, gl, hosts, looks, proj in SAME:
        want = {"gitlab": gl, "git_hosts": hosts, "looks_like": looks,
                "project_of": proj}
        got = consumers(url)
        for k in want:
            cases += 1
            if got[k] != want[k]:
                fail(f"{k}({url!r}) -> {got[k]!r}, wanted {want[k]!r}")

    for url, who, was, now in CHANGED:
        cases += 1
        got = consumers(url)[who]
        if got != now:
            fail(f"{who}({url!r}) -> {got!r}, wanted {now!r} (was {was!r})")

    parse = getattr(driver, "parse_origin", None)
    for url, want in PARSE:
        cases += 1
        got = parse(url) if parse else "no driver.parse_origin"
        if got != want:
            fail(f"parse_origin({url!r}) -> {got!r}, wanted {want!r}")

    # Правило проекта -- одно, у драйвера: агент puppets импортировать не
    # может, и раньше держал копию.
    cases += 1
    if getattr(driver, "project_of", None) is not puppets.project_of:
        fail("puppets.project_of must be driver.project_of")
    cases += 1
    copies = []
    for top, _, files in os.walk(os.path.join(ROOT, "mop")):
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(top, f)
                with open(path) as fh:
                    if 'removesuffix(".git")' in fh.read():
                        copies.append(os.path.relpath(path, ROOT))
    if copies != [os.path.join("mop", "driver", "__init__.py")]:
        fail(f"project rule must live only in mop/driver/__init__.py, found in {copies}")

    # HYPOTHESIS (#165): project_of брал basename всей строки, и на
    # вырожденных формах выходил мусор: scp без группы -- «git@h:p», хвостовой
    # слеш -- пустой проект.
    # SOLUTION: путь -- из parse_origin, если разбирается; иначе прежний
    # ответ. Реальные формы (SAME выше) не сдвинулись.
    # STATUS: FIXED — see #165
    for url, want in (("git@h:p.git", "p"), ("git@h:p", "p"),
                      ("https://h/g/p/", "p"), ("ssh://git@h:2222/g/p.git/", "p"),
                      ("x:", "x:"), ("mop", "mop"), ("", "")):
        cases += 1
        if driver.project_of(url) != want:
            fail(f"project_of({url!r}) -> {driver.project_of(url)!r}, wanted {want!r}")
    # Прежний мусор именем папета не становился: valid_name его не пускает.
    for n in ("pu-git@h:p-1", "pu--1"):
        cases += 1
        if driver.valid_name(n):
            fail(f"valid_name({n!r}) accepted an old garbage name")

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
