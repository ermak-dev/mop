#!/usr/bin/env python3
"""Проверка реестра проектов без кластера: python3 tests/projects.py

Реестр — единственный ответ на вопрос «какие проекты заведены»: по нему
плейбук рендерит пользователей NATS. Пока его не было, список собирался
объединением ростера, памяти контроллера и аргументов `mop server deploy`, и завод
проекта был побочным эффектом прогона (#79).

Здесь проверяется чистая часть: что реестр принимает, что отдаёт и что
теряет. Всё остальное — чтение файла и прогон ansible.

HYPOTHESIS (#79): списка проектов как сущности нет — он собирается при каждом
прогоне из ростера, памяти контроллера и аргументов, поэтому завод проекта
есть побочный эффект `mop server deploy <origin>`, а снятия нет вовсе.
SOLUTION: реестр ~/.config/mop/projects, группа `mop project add|delete|list`
и узкий прогон deploy/projects.yml; deploy реестр только читает.
STATUS: FIXED — see #79
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import projects  # noqa: E402

RU = "git@git.ermak.dev:ermak/rudesktop.git"
MOP = "git@git.ermak.dev:ermak/mop.git"
OTHER = "https://example.org/team/rudesktop.git"


def check_add(c):
    """with_origin: что реестр принимает."""
    got, added = projects.with_origin(MOP, {RU})
    c.check(f"new origin not added: {got}, added={added}", not (got != {RU, MOP} or not added))

    # Повтор — не ошибка и не изменение: `mop project add` идемпотентен,
    # иначе повторный завод проекта читался бы как отказ.
    got, added = projects.with_origin(RU, {RU})
    c.check(f"duplicate origin changed the registry: {got}, added={added}",
            not (got != {RU} or added))

    # Голое имя отвергается: origin — единственная правда о проекте, из
    # имени его не развернуть, а deploy режет манифест именно из origin.
    # Легаси-строки в реестре живут, но завести новую такую нельзя.
    for bad in ("rudesktop", "", "   "):
        try:
            projects.with_origin(bad, set())
            refused = False
        except RuntimeError:
            refused = True
        c.check(f"bare name {bad!r} accepted as an origin", refused)


def check_delete(c):
    """without_project: что реестр теряет."""
    got, dropped = projects.without_project("rudesktop", {RU, MOP})
    c.check(f"delete by project name: {got}, dropped={dropped}",
            not (got != {MOP} or dropped != [RU]))

    # Имя проекта — basename origin'а, и оно же имя пользователя NATS.
    # Два origin'а с одним basename — один проект: снимать надо оба, иначе
    # реестр помнит проект, которого на шине уже нет.
    got, dropped = projects.without_project("rudesktop", {RU, OTHER, MOP})
    c.check(f"two origins of one project: {got}, dropped={dropped}",
            not (got != {MOP} or dropped != sorted([RU, OTHER])))

    # Легаси-строка — имя без origin'а: снимается по себе самой.
    got, dropped = projects.without_project("oldproject", {"oldproject", MOP})
    c.check(f"legacy name not dropped: {got}, dropped={dropped}",
            not (got != {MOP} or dropped != ["oldproject"]))

    # Неизвестный проект — пустой вердикт, а не молчаливое «сделано»:
    # опечатка в имени иначе читалась бы как успешное снятие.
    got, dropped = projects.without_project("nosuch", {RU})
    c.check(f"unknown project reported as dropped: {got}, dropped={dropped}",
            not (got != {RU} or dropped != []))


def check_names(c):
    """names: имена проектов для плейбука — basename'ы плюс легаси."""
    got = projects.names({RU, MOP}, {"oldproject"})
    c.check(f"names: {got}", not (got != ["mop", "oldproject", "rudesktop"]))
    # Два origin'а одного проекта дают ОДНО имя: пользователь в конфиге
    # NATS заводится по имени, и дубль в цикле плейбука — второй lookup
    # того же пароля.
    got = projects.names({RU, OTHER}, set())
    c.check(f"duplicate basenames: {got}", not (got != ["rudesktop"]))


def check_limits(c):
    """Потолок папетов проекта (#107): что принимается и как снимается.

    HYPOTHESIS: лимитов нет, проект занимает пул, пока не кончатся слоты.
    SOLUTION: лимит -- политика проекта в своём файле рядом с реестром;
    сервис кластера сверяет с ним `add`.
    STATUS: FIXED — see #107
    """
    # Число и снятие. 0 законен: «новых папетов не заводить» -- заморозка.
    for text, want in (("3", 3), ("0", 0), ("none", None)):
        try:
            got = projects.parse_limit(text)
        except ValueError as e:
            c.fail(f"parse_limit({text!r}) refused: {e}")
            continue
        c.check(f"parse_limit({text!r}) -> {got!r}, wanted {want!r}", not (got != want))
    # Мусор -- отказ, а не «без лимита»: опечатка снимала бы потолок молча.
    for bad in ("", "-1", "three", "2.5"):
        try:
            projects.parse_limit(bad)
            refused = False
        except ValueError:
            refused = True
        c.check(f"parse_limit({bad!r}) must be refused", refused)

    got = projects.with_limit({"mop": 2}, "rugent", 5)
    c.check(f"with_limit set -> {got}", not (got != {"mop": 2, "rugent": 5}))
    got = projects.with_limit({"mop": 2, "rugent": 5}, "rugent", None)
    c.check(f"with_limit clear -> {got}", not (got != {"mop": 2}))
    # Исходный словарь не трогаем: чистая функция.
    src = {"mop": 2}
    projects.with_limit(src, "mop", None)
    c.check("with_limit must not change its argument", not (src != {"mop": 2}))


def check_git_hosts(c):
    """HYPOTHESIS (#121): узел доверял ключу хоста одного MOP_GIT_HOST, и
    папет проекта с другого форжа падал на клоне: Host key verification
    failed. SOLUTION: хосты для known_hosts -- MOP_GIT_HOST плюс ssh-хосты
    origin'ов реестра. STATUS: FIXED — see #121"""
    try:
        fn = projects.git_hosts
    except AttributeError:
        c.fail("projects.git_hosts is missing")
        return
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
        c.check(f"git_hosts({origins!r}, {default!r}) -> {got!r}, wanted {want!r}",
                not (got != want))


def check_for_deploy(c):
    """HYPOTHESIS (#117): `mop server deploy` читал реестр контроллера, а правда о
    проектах теперь -- реестр сервера, который правят глаголы сервиса.
    SOLUTION: deploy спрашивает сервер; нет ответа (чистая установка: шины
    ещё нет) -- берёт копию этой машины и говорит об этом.
    STATUS: FIXED — see #117"""
    try:
        fn = projects.for_deploy
    except AttributeError:
        c.fail("projects.for_deploy is missing")
        return
    lines, note = fn({"ok": True, "lines": ["git@h:g/mop.git", "legacy"]}, {"git@h:g/old.git"})
    c.check(f"a server answer must win silently: {lines} {note!r}",
            not (lines != {"git@h:g/mop.git", "legacy"} or note))
    lines, note = fn({"error": "no cluster service"}, {"git@h:g/old.git"})
    c.check(f"without the server the local copy is used, and said so: {lines} {note!r}",
            not (lines != {"git@h:g/old.git"} or not note or "no cluster service" not in note))
    # Пустой ответ сервера -- правда (проектов нет), а не повод взять копию.
    lines, note = fn({"ok": True, "lines": []}, {"git@h:g/old.git"})
    c.check(f"an empty server registry is the truth: {lines}", not (lines != set()))


def check_read_limits_337(c):
    """HYPOTHESIS (#337): read_limits зовёт int() на каждом значении и ловит
    OSError, ValueError, AttributeError, но не TypeError: `null`, список или
    объект в файле лимитов роняют глагол сервиса (add, снимок, `mop project
    limit`) исключением -- одна плохая строка кладёт add всего пула.
    SOLUTION: по записи, не по файлу -- значение, которое не целое (null,
    список, объект, bool, нечисловая строка), пропускается, потолки
    остальных проектов действуют: потолок защищает, и одна строка не должна
    снимать все. STATUS: FIXED — see #337"""
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="mop-test-limits-337-")
    path = os.path.join(d, "limits.json")
    try:
        c.expect("#337 a missing limits file reads as {}", projects.read_limits(path), {})
        for text, want in (('{"a": null, "b": 3}', {"b": 3}),
                           ('{"a": [], "b": {}}', {}),
                           ('{"a": true}', {}),
                           ('{"a": "x", "b": 2}', {"b": 2}),
                           ('{"a": "5"}', {"a": 5}),
                           ('{"a": 5}', {"a": 5}),
                           ('{"a": Infinity, "b": 1}', {"b": 1}),
                           ('[1, 2]', {}),
                           ('{broken', {})):
            with open(path, "w") as f:
                f.write(text)
            try:
                got = projects.read_limits(path)
            except Exception as e:
                got = f"raised {type(e).__name__}: {e}"
            c.expect(f"#337 read_limits({text})", got, want)
    finally:
        shutil.rmtree(d)


def main():
    c = Checks()
    for fn in (check_add, check_delete, check_names, check_limits, check_read_limits_337,
               check_git_hosts, check_for_deploy):
        fn(c)
    return c.report("projects")


if __name__ == "__main__":
    sys.exit(main())
