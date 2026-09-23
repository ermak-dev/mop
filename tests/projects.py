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


def check_merge():
    """merged: разовый перенос памяти и ростера в реестр.

    Память контроллера хранит origin'ы и легаси-имена, ростер — origin'ы
    живых папетов. Потерять хоть одну строку значит выписать проект из
    конфига NATS на следующем прогоне, поэтому перенос — объединение.
    """
    out = []
    got = projects.merged({RU, "oldproject"}, {MOP, RU})
    if got != {RU, MOP, "oldproject"}:
        out.append(f"merge lost a line: {got}")

    got = projects.merged(set(), set())
    if got != set():
        out.append(f"empty install is not empty: {got}")
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


def main():
    failed = []
    for check in (check_add, check_delete, check_merge, check_names):
        for line in check():
            failed.append(f"FAIL {check.__name__}: {line}")
    print("\n".join(failed) if failed else "", end="\n" if failed else "")
    print("projects: FAILED" if failed else "projects: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
