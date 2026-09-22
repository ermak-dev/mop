#!/usr/bin/env python3
"""Что ставит mop setup, без установки: python3 tests/deps.py

Проверяется план, а не установка: apt и pip на живой машине не прогонишь в
проверке, а ошибка в плане молчит особенно охотно — недостающий пакет
проявится на первом `mop deploy` как «command not found» на другой машине.

HYPOTHESIS (#55): зависимости контроллера и оператора нигде не записаны и
ставятся руками по одной, по подсказкам из падений.
SOLUTION: один список в mop/deps.py, план по роли и по тому, что уже есть.
STATUS: FIXED — see #55
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import deps  # noqa: E402


def main():
    bad = 0
    cases = 0

    nothing = lambda _: False   # noqa: E731
    everything = lambda _: True  # noqa: E731

    # Контроллер с голой машины: всё.
    cases += 1
    got = deps.plan("controller", nothing, nothing)
    want = {"apt": ["ansible", "rsync", "git", "curl"],
            "galaxy": ["ansible.posix"],
            "pip": ["nats-py", "python-nomad", "requests", "mcp", "pyyaml"]}
    if got != want:
        bad += 1
        print(f"FAILED  bare controller -> {got}, wanted {want}")

    # Оператор: без ansible и rsync, pip тот же — mop mcp и join ходят на
    # те же библиотеки, что и контроллер.
    cases += 1
    got = deps.plan("operator", nothing, nothing)
    want = {"apt": ["git"], "galaxy": [],
            "pip": ["nats-py", "python-nomad", "requests", "mcp", "pyyaml"]}
    if got != want:
        bad += 1
        print(f"FAILED  bare operator -> {got}, wanted {want}")

    # Повторный запуск: ничего. Модуль проверяется по имени импорта, а не
    # по имени пакета — pyyaml импортируется как yaml, nats-py как nats.
    cases += 1
    got = deps.plan("controller", everything, everything)
    if got != {"apt": [], "galaxy": [], "pip": []}:
        bad += 1
        print(f"FAILED  second run must be a no-op: {got}")

    cases += 1
    have_mod = lambda m: m in {"nats", "nomad", "requests", "mcp"}  # noqa: E731
    got = deps.plan("operator", everything, have_mod)
    if got["pip"] != ["pyyaml"]:
        bad += 1
        print(f"FAILED  pyyaml is the module yaml, and it is missing here: {got['pip']}")

    # Коллекция считается командой ansible-galaxy: проверять её без ansible
    # нечем, и при отсутствующем ansible она идёт в план вместе с ним.
    cases += 1
    have_cmd = lambda c: c != "ansible.posix"  # noqa: E731
    got = deps.plan("controller", have_cmd, everything)
    if got != {"apt": [], "galaxy": ["ansible.posix"], "pip": []}:
        bad += 1
        print(f"FAILED  only the collection is missing: {got}")

    cases += 1
    try:
        deps.plan("node", nothing, nothing)
        bad += 1
        print("FAILED  nodes are ansible's business: an unknown role must refuse")
    except ValueError:
        pass

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
