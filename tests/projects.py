#!/usr/bin/env python3
"""Проверка реестра проектов без кластера: python3 tests/projects.py

Реестр — единственный ответ на вопрос «какие проекты заведены»: по нему
плейбук рендерит пользователей NATS. Пока его не было, список собирался
объединением ростера, памяти контроллера и аргументов `mop deploy`, и завод
проекта был побочным эффектом прогона (#79).

Здесь проверяется чистая часть: что реестр принимает, что отдаёт и что
теряет. Всё остальное — чтение файла и прогон ansible.

HYPOTHESIS (#79): списка проектов как сущности нет — он собирается при каждом
прогоне из ростера, памяти контроллера и аргументов, поэтому завод проекта
есть побочный эффект `mop deploy <origin>`, а снятия нет вовсе.
SOLUTION: реестр ~/.config/mop/projects, группа `mop project add|delete|list`
и узкий прогон deploy/projects.yml; deploy реестр только читает.
STATUS: FIXED — see #79
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import projects  # noqa: E402

RU = "git@git.ermak.dev:ermak/rudesktop.git"
MOP = "git@git.ermak.dev:ermak/mop.git"
OTHER = "https://example.org/team/rudesktop.git"


def check_add():
    """with_origin: что реестр принимает."""
    out = []

    got, added = projects.with_origin(MOP, {RU})
    if got != {RU, MOP} or not added:
        out.append(f"new origin not added: {got}, added={added}")

    # Повтор — не ошибка и не изменение: `mop project add` идемпотентен,
    # иначе повторный завод проекта читался бы как отказ.
    got, added = projects.with_origin(RU, {RU})
    if got != {RU} or added:
        out.append(f"duplicate origin changed the registry: {got}, added={added}")

    # Голое имя отвергается: origin — единственная правда о проекте, из
    # имени его не развернуть, а deploy режет манифест именно из origin.
    # Легаси-строки в реестре живут, но завести новую такую нельзя.
    for bad in ("rudesktop", "", "   "):
        try:
            projects.with_origin(bad, set())
        except RuntimeError:
            continue
        out.append(f"bare name {bad!r} accepted as an origin")
    return out


def check_delete():
    """without_project: что реестр теряет."""
    out = []

    got, dropped = projects.without_project("rudesktop", {RU, MOP})
    if got != {MOP} or dropped != [RU]:
        out.append(f"delete by project name: {got}, dropped={dropped}")

    # Имя проекта — basename origin'а, и оно же имя пользователя NATS.
    # Два origin'а с одним basename — один проект: снимать надо оба, иначе
    # реестр помнит проект, которого на шине уже нет.
    got, dropped = projects.without_project("rudesktop", {RU, OTHER, MOP})
    if got != {MOP} or dropped != sorted([RU, OTHER]):
        out.append(f"two origins of one project: {got}, dropped={dropped}")

    # Легаси-строка — имя без origin'а: снимается по себе самой.
    got, dropped = projects.without_project("oldproject", {"oldproject", MOP})
    if got != {MOP} or dropped != ["oldproject"]:
        out.append(f"legacy name not dropped: {got}, dropped={dropped}")

    # Неизвестный проект — пустой вердикт, а не молчаливое «сделано»:
    # опечатка в имени иначе читалась бы как успешное снятие.
    got, dropped = projects.without_project("nosuch", {RU})
    if got != {RU} or dropped != []:
        out.append(f"unknown project reported as dropped: {got}, dropped={dropped}")
    return out


def check_names():
    """names: имена проектов для плейбука — basename'ы плюс легаси."""
    out = []
    got = projects.names({RU, MOP}, {"oldproject"})
    if got != ["mop", "oldproject", "rudesktop"]:
        out.append(f"names: {got}")
    # Два origin'а одного проекта дают ОДНО имя: пользователь в конфиге
    # NATS заводится по имени, и дубль в цикле плейбука — второй lookup
    # того же пароля.
    got = projects.names({RU, OTHER}, set())
    if got != ["rudesktop"]:
        out.append(f"duplicate basenames: {got}")
    return out


def check_limits():
    """Потолок папетов проекта (#107): что принимается и как снимается.

    HYPOTHESIS: лимитов нет, проект занимает пул, пока не кончатся слоты.
    SOLUTION: лимит -- политика проекта в своём файле рядом с реестром;
    сервис кластера сверяет с ним `add`.
    STATUS: FIXED — see #107
    """
    out = []
    # Число и снятие. 0 законен: «новых папетов не заводить» -- заморозка.
    for text, want in (("3", 3), ("0", 0), ("none", None)):
        try:
            got = projects.parse_limit(text)
        except ValueError as e:
            out.append(f"parse_limit({text!r}) refused: {e}")
            continue
        if got != want:
            out.append(f"parse_limit({text!r}) -> {got!r}, wanted {want!r}")
    # Мусор -- отказ, а не «без лимита»: опечатка снимала бы потолок молча.
    for bad in ("", "-1", "three", "2.5"):
        try:
            projects.parse_limit(bad)
        except ValueError:
            continue
        out.append(f"parse_limit({bad!r}) must be refused")

    got = projects.with_limit({"mop": 2}, "rugent", 5)
    if got != {"mop": 2, "rugent": 5}:
        out.append(f"with_limit set -> {got}")
    got = projects.with_limit({"mop": 2, "rugent": 5}, "rugent", None)
    if got != {"mop": 2}:
        out.append(f"with_limit clear -> {got}")
    # Исходный словарь не трогаем: чистая функция.
    src = {"mop": 2}
    projects.with_limit(src, "mop", None)
    if src != {"mop": 2}:
        out.append("with_limit must not change its argument")
    return out


def check_git_hosts():
    """HYPOTHESIS (#121): узел доверял ключу хоста одного MOP_GIT_HOST, и
    папет проекта с другого форжа падал на клоне: Host key verification
    failed. SOLUTION: хосты для known_hosts -- MOP_GIT_HOST плюс ssh-хосты
    origin'ов реестра. STATUS: FIXED — see #121"""
    out = []
    try:
        fn = projects.git_hosts
    except AttributeError:
        return ["projects.git_hosts is missing"]
    cases = [
        # Проект на форже установки -- один хост, без дубля.
        (["git@dev.corp:rud/app.git"], "dev.corp", ["dev.corp"]),
        # Проект с чужого форжа -- его хост добавляется.
        (["git@git.ermak.dev:ermak/mop.git", "git@dev.corp:rud/app.git"],
         "dev.corp", ["dev.corp", "git.ermak.dev"]),
        # ssh:// с портом: known_hosts пишет такой хост как [host]:port.
        (["ssh://git@forge.example:2222/team/x.git"], "dev.corp",
         ["dev.corp", "[forge.example]:2222"]),
        (["ssh://git@forge.example/team/x.git"], "dev.corp",
         ["dev.corp", "forge.example"]),
        # https и локальный путь ключа хоста не требуют.
        (["https://github.com/a/b.git", "/srv/git/c.git"], "dev.corp",
         ["dev.corp"]),
        # Пустой реестр -- хост установки всё равно нужен: им клонирует сборка.
        ([], "dev.corp", ["dev.corp"]),
        # HYPOTHESIS (#166): git клонирует git+ssh:// и ssh+git:// по ssh, а
        # git_hosts их хостов не давал -- узел не знал ключа форжа, и папет
        # падал на клоне «Host key verification failed» (как #121/#141).
        # SOLUTION: схемы ssh, git+ssh, ssh+git -- все ssh. STATUS: FIXED — see #166
        (["git+ssh://git@forge.example/team/x.git"], "dev.corp",
         ["dev.corp", "forge.example"]),
        (["git+ssh://git@forge.example:2222/team/x.git"], "dev.corp",
         ["dev.corp", "[forge.example]:2222"]),
        (["ssh+git://forge.example/team/x.git"], "dev.corp",
         ["dev.corp", "forge.example"]),
        (["ssh+git://git@forge.example:22/team/x.git"], "dev.corp",
         ["dev.corp", "forge.example"]),
        (["ssh+git://git@forge.example:2222/team/x"], "dev.corp",
         ["dev.corp", "[forge.example]:2222"]),
        # https по-прежнему не ssh.
        (["https://forge.example:8443/team/x.git"], "dev.corp", ["dev.corp"]),
    ]
    for origins, default, want in cases:
        got = fn(origins, default)
        if got != want:
            out.append(f"git_hosts({origins!r}, {default!r}) -> {got!r}, wanted {want!r}")
    return out


def check_for_deploy():
    """HYPOTHESIS (#117): `mop deploy` читал реестр контроллера, а правда о
    проектах теперь -- реестр сервера, который правят глаголы сервиса.
    SOLUTION: deploy спрашивает сервер; нет ответа (чистая установка: шины
    ещё нет) -- берёт копию этой машины и говорит об этом.
    STATUS: FIXED — see #117"""
    out = []
    try:
        fn = projects.for_deploy
    except AttributeError:
        return ["projects.for_deploy is missing"]
    lines, note = fn({"ok": True, "lines": ["git@h:g/mop.git", "legacy"]}, {"git@h:g/old.git"})
    if lines != {"git@h:g/mop.git", "legacy"} or note:
        out.append(f"a server answer must win silently: {lines} {note!r}")
    lines, note = fn({"error": "no cluster service"}, {"git@h:g/old.git"})
    if lines != {"git@h:g/old.git"} or not note or "no cluster service" not in note:
        out.append(f"without the server the local copy is used, and said so: {lines} {note!r}")
    # Пустой ответ сервера -- правда (проектов нет), а не повод взять копию.
    lines, note = fn({"ok": True, "lines": []}, {"git@h:g/old.git"})
    if lines != set():
        out.append(f"an empty server registry is the truth: {lines}")
    return out


def main():
    failed = []
    for check in (check_add, check_delete, check_names, check_limits,
                  check_git_hosts, check_for_deploy):
        for line in check():
            failed.append(f"FAIL {check.__name__}: {line}")
    print("\n".join(failed) if failed else "", end="\n" if failed else "")
    print("projects: FAILED" if failed else "projects: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
