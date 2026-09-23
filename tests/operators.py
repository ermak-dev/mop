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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import operators  # noqa: E402


def check_parse():
    """Разбор настройки: кто есть и на что имеет право."""
    out = []
    got = operators.parse("anton:mop,rugent; ivan:cloudpub ;  ")
    want = {"anton": ["mop", "rugent"], "ivan": ["cloudpub"]}
    if got != want:
        out.append(f"parse -> {got}, wanted {want}")

    # Звёздочка — все проекты, как у оператора сегодня. Пишется явно: пустой
    # список прав значил бы «на всё» ровно там, где опечатка даёт доступ.
    if operators.parse("anton:*") != {"anton": ["*"]}:
        out.append(f"star -> {operators.parse('anton:*')}")

    if operators.parse("") != {}:
        out.append("an empty setting must give no operators, not an error")

    # Имя без проектов — отказ, а не «на всё»: прав по умолчанию не бывает.
    for bad in ("anton", "anton:", ":mop"):
        try:
            operators.parse(bad)
        except ValueError:
            continue
        out.append(f"{bad!r} must be refused: rights are never implied")
    return out


def check_reserved():
    """Имя человека не должно совпасть с ролевым: это тихая подмена роли."""
    out = []
    # service (#104) -- машинный пользователь сервисов сервера: человек с
    # этим именем получил бы права сервисов под видом оператора.
    for bad in ("admin", "service", "master-mop", "puppet-mop", "node-mate"):
        try:
            operators.parse(f"{bad}:mop")
        except ValueError:
            continue
        out.append(f"{bad!r} must be refused as an operator name")
    # Обычное человеческое имя проходит.
    if operators.parse("anton:mop") != {"anton": ["mop"]}:
        out.append("an ordinary name must be accepted")
    return out


def check_subjects():
    """Права пользователя: свои проекты и инбоксы, и ничего сверх."""
    out = []
    got = operators.subjects(["mop", "rugent"])
    if got != ["mop.mop.>", "mop.rugent.>", "_INBOX.>"]:
        out.append(f"subjects -> {got}")
    # `*` — весь mop.>, как у admin: оператору всего пула нечего перечислять.
    if operators.subjects(["*"]) != ["mop.>", "_INBOX.>"]:
        out.append(f"star subjects -> {operators.subjects(['*'])}")
    # _INBOX обязателен: без него request-reply молча не работает.
    if "_INBOX.>" not in operators.subjects(["mop"]):
        out.append("_INBOX.> is mandatory or request-reply silently fails")
    return out


def main():
    failed = []
    for check in (check_parse, check_reserved, check_subjects):
        for line in check():
            failed.append(f"FAIL {check.__name__}: {line}")
    if failed:
        print("\n".join(failed))
    print("operators: FAILED" if failed else "operators: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
