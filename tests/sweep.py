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

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import puppets  # noqa: E402
from mop.common.domain import CloneFacts  # noqa: E402


def facts(cur, dirty=0, ahead=0, home=None, default="master"):
    """Факты клона в той форме, в какой их отдаёт агент (CloneFacts.to_dict)."""
    return CloneFacts(branch=cur, default_branch=default, home=home,
                      origin="git@h:g/a.git", dirty=dirty, ahead=ahead).to_dict()


def main():
    c = Checks()

    # Тело, которого нет среди джобов, — сирота. Тело, которое есть, — нет.
    answers = {"hyper": {"bodies": ["pu-a-1", "pu-b-2"], "templates": []}}
    rows = puppets.classify_junk(answers, {"pu-a-1"})
    c.expect("one orphan found", [r["name"] for r in rows], ["pu-b-2"])
    c.expect("an orphan is sweepable", [r["sweepable"] for r in rows], [True])

    # Пустой список джобов — это НЕ «все тела сироты». Так выглядит Nomad,
    # который не ответил. Функция чистая и верит тому, что ей дали, поэтому
    # предохранитель стоит у вызывающего (bin/sweep отказывается работать с
    # пустым реестром); здесь фиксируем, что без него вышло бы.
    rows = puppets.classify_junk(answers, set())
    c.expect("with no jobs everything reads as orphaned",
          len([r for r in rows if r["sweepable"]]), 2)

    # Сирота с несохранённой работой НЕ сносится. Это главный предохранитель:
    # джоба у неё нет и к делу её уже не вернуть, но снесённое не
    # возвращается вовсе, а лежащее тело стоит только места.
    # С #372 -- полная форма фактов клона, как её отдаёт агент: прежняя
    # урезанная {dirty, ahead, cur} закрепляла дефект (без def/home правило
    # «не на своей ветке» не срабатывало никогда).
    answers_work = {"hyper": {
        "bodies": ["pu-a-1", "pu-b-2", "pu-c-3"],
        "work": {"pu-a-1": facts("bug/1", dirty=3, home="bug/1"),
                 "pu-b-2": facts("feat/2", ahead=2, home="feat/2"),
                 "pu-c-3": facts("master")}}}
    rows = puppets.classify_junk(answers_work, set())
    c.expect("uncommitted work is spared",
          [r["sweepable"] for r in rows if r["name"] == "pu-a-1"], [False])
    c.expect("unpushed work is spared",
          [r["sweepable"] for r in rows if r["name"] == "pu-b-2"], [False])
    c.expect("a clean orphan is still swept",
          [r["sweepable"] for r in rows if r["name"] == "pu-c-3"], [True])
    c.expect("the reason names the numbers",
          "3 uncommitted" in [r for r in rows if r["name"] == "pu-a-1"][0]["detail"],
          True)

    # Нет сведений о работе — не то же самое, что «работы нет»… но и отказ
    # здесь был бы неверен: у драйвера, чьё тело недостижимо, сведений не
    # будет никогда, и уборка встала бы навсегда. Фиксируем выбор явно.
    rows = puppets.classify_junk({"n": {"bodies": ["pu-x-1"]}}, set())
    c.expect("no work data means the body is treated as clean",
          [r["sweepable"] for r in rows], [True])

    # Работающий шаблон называется, но не сносится: отсюда не отличить
    # идущую сборку от оборванной.
    answers = {"hyper": {"bodies": [], "templates": [
        {"name": "pu-tmpl-x", "vmid": "9963", "running": True},
        {"name": "pu-tmpl-y", "vmid": "9900", "running": False}]}}
    rows = puppets.classify_junk(answers, set())
    c.expect("only the running template is reported",
          [r["name"] for r in rows], ["pu-tmpl-x"])
    c.expect("a build body is never swept automatically",
          [r["sweepable"] for r in rows], [False])

    # Узел без тел не выдумывает находок, и драйвер без шаблонов тоже.
    c.expect("an empty node yields nothing",
          puppets.classify_junk({"mate": {"bodies": [], "templates": []}}, set()), [])
    c.expect("a driver with no templates key is fine",
          puppets.classify_junk({"mate": {"bodies": []}}, set()), [])
    c.expect("a node that answered nothing yields nothing",
          puppets.classify_junk({"mate": None}, set()), [])

    # Порядок устойчив: вывод читает человек, и строки не должны прыгать
    # между прогонами.
    answers = {"b": {"bodies": ["pu-z-9", "pu-a-1"], "templates": []},
               "a": {"bodies": ["pu-m-3"], "templates": []}}
    rows = puppets.classify_junk(answers, set())
    c.expect("nodes and names come out sorted",
          [(r["node"], r["name"]) for r in rows],
          [("a", "pu-m-3"), ("b", "pu-a-1"), ("b", "pu-z-9")])

    check_v_junk_372(c)
    return c.report("sweep")


# ── #372: уборка сирот видит правило «клон не на своей ветке» ──────────
# HYPOTHESIS: v_junk агента отдаёт урезанные факты {dirty, ahead, cur} без
# def/home/owner, хотя clone_facts уже возвращает полный CloneFacts; на
# клиенте CloneFacts.from_dict получает клон без дома, и правило #266/#272
# «клон не на своей ветке -- работа» не срабатывает: чистую запушенную
# сироту на чужой ветке уборка сносит.
# SOLUTION: v_junk отдаёт clone_facts как есть -- полный to_dict().
# STATUS: FIXED — see #372
def check_v_junk_372(c):
    import asyncio
    from _lib import patched, restored
    from mop.node import agent

    class Drv:
        async def bodies(self):
            return ["pu-a-1", "pu-a-2"]
    probed = {"pu-a-1": facts("feat/9", home="master"),     # чистый, запушен, не дома
              "pu-a-2": facts("master", home="master")}      # дома и чист

    async def clone_facts(name):
        return dict(probed[name])
    with patched(agent, DRIVER=Drv()), restored(agent, "clone_facts"):
        agent.clone_facts = clone_facts
        reply = asyncio.run(agent.v_junk(None, {}))
    c.expect("#372 v_junk carries the full clone facts",
             reply["work"].get("pu-a-1"), probed["pu-a-1"])
    rows = {r["name"]: r for r in puppets.classify_junk({"hyper": reply}, set())}
    off = rows.get("pu-a-1") or {}
    c.check(f"#372 a clean pushed orphan off its home branch is spared: {off}",
            off.get("sweepable") is False and "off home master" in (off.get("detail") or ""))
    c.expect("#372 a clean orphan at home is swept",
             (rows.get("pu-a-2") or {}).get("sweepable"), True)


if __name__ == "__main__":
    sys.exit(main())
