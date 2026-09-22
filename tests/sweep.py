#!/usr/bin/env python3
"""Что уборка считает мусором: python3 tests/sweep.py

Решение «снести тело» обратной команды не имеет, а ошибка в нём стоит чужой
работы — значит оно обязано быть чистой функцией и проверяться без пула.
Живой пул тут не помощник: чтобы увидеть сироту, на нём надо сперва её
завести.

Поводом был настоящий мусор, найденный 22.09 на hyper: контейнер
pu-rugent-2 работал, держа 118 ГБ тонкого тома, а джоба у него не было уже
несколько часов — `mop delete` тело не трогал вовсе.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import puppets  # noqa: E402


def main():
    cases = bad = 0

    def check(what, got, want):
        nonlocal cases, bad
        cases += 1
        if got != want:
            bad += 1
            print(f"FAILED  {what}: {got!r} != {want!r}")

    # Тело, которого нет среди джобов, — сирота. Тело, которое есть, — нет.
    answers = {"hyper": {"bodies": ["pu-a-1", "pu-b-2"], "templates": []}}
    rows = puppets.classify_junk(answers, {"pu-a-1"})
    check("one orphan found", [r["name"] for r in rows], ["pu-b-2"])
    check("an orphan is sweepable", [r["sweepable"] for r in rows], [True])

    # Пустой список джобов — это НЕ «все тела сироты». Так выглядит Nomad,
    # который не ответил. Функция чистая и верит тому, что ей дали, поэтому
    # предохранитель стоит у вызывающего (bin/sweep отказывается работать с
    # пустым реестром); здесь фиксируем, что без него вышло бы.
    rows = puppets.classify_junk(answers, set())
    check("with no jobs everything reads as orphaned",
          len([r for r in rows if r["sweepable"]]), 2)

    # Сирота с несохранённой работой НЕ сносится. Это главный предохранитель:
    # джоба у неё нет и к делу её уже не вернуть, но снесённое не
    # возвращается вовсе, а лежащее тело стоит только места.
    answers_work = {"hyper": {
        "bodies": ["pu-a-1", "pu-b-2", "pu-c-3"],
        "work": {"pu-a-1": {"dirty": 3, "ahead": 0, "cur": "bug/1"},
                 "pu-b-2": {"dirty": 0, "ahead": 2, "cur": "feat/2"},
                 "pu-c-3": {"dirty": 0, "ahead": 0, "cur": "master"}}}}
    rows = puppets.classify_junk(answers_work, set())
    check("uncommitted work is spared",
          [r["sweepable"] for r in rows if r["name"] == "pu-a-1"], [False])
    check("unpushed work is spared",
          [r["sweepable"] for r in rows if r["name"] == "pu-b-2"], [False])
    check("a clean orphan is still swept",
          [r["sweepable"] for r in rows if r["name"] == "pu-c-3"], [True])
    check("the reason names the numbers",
          "3 uncommitted" in [r for r in rows if r["name"] == "pu-a-1"][0]["detail"],
          True)

    # Нет сведений о работе — не то же самое, что «работы нет»… но и отказ
    # здесь был бы неверен: у драйвера, чьё тело недостижимо, сведений не
    # будет никогда, и уборка встала бы навсегда. Фиксируем выбор явно.
    rows = puppets.classify_junk({"n": {"bodies": ["pu-x-1"]}}, set())
    check("no work data means the body is treated as clean",
          [r["sweepable"] for r in rows], [True])

    # Работающий шаблон называется, но не сносится: отсюда не отличить
    # идущую сборку от оборванной.
    answers = {"hyper": {"bodies": [], "templates": [
        {"name": "pu-tmpl-x", "vmid": "9963", "running": True},
        {"name": "pu-tmpl-y", "vmid": "9900", "running": False}]}}
    rows = puppets.classify_junk(answers, set())
    check("only the running template is reported",
          [r["name"] for r in rows], ["pu-tmpl-x"])
    check("a build body is never swept automatically",
          [r["sweepable"] for r in rows], [False])

    # Узел без тел не выдумывает находок, и драйвер без шаблонов тоже.
    check("an empty node yields nothing",
          puppets.classify_junk({"mate": {"bodies": [], "templates": []}}, set()), [])
    check("a driver with no templates key is fine",
          puppets.classify_junk({"mate": {"bodies": []}}, set()), [])
    check("a node that answered nothing yields nothing",
          puppets.classify_junk({"mate": None}, set()), [])

    # Порядок устойчив: вывод читает человек, и строки не должны прыгать
    # между прогонами.
    answers = {"b": {"bodies": ["pu-z-9", "pu-a-1"], "templates": []},
               "a": {"bodies": ["pu-m-3"], "templates": []}}
    rows = puppets.classify_junk(answers, set())
    check("nodes and names come out sorted",
          [(r["node"], r["name"]) for r in rows],
          [("a", "pu-m-3"), ("b", "pu-a-1"), ("b", "pu-z-9")])

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
