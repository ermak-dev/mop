#!/usr/bin/env python3
"""Проверка суждений о узлах без кластера: python3 tests/node.py

Здесь одно, но дорогое: можно ли убрать узел из ростера. `forget` на узле с
работающими аллокациями — это снос узла из-под ЖИВЫХ папетов, и обратной
команды у него нет. Решение обязано быть чистой функцией и проверяться без
кластера: на живом ошибиться можно ровно один раз.

Это не фреймворк и не прогон всего проекта: остальное по-прежнему добывается
на живом пуле.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import nomad  # noqa: E402


def node(status="ready", elig="ineligible", drain=False):
    return {"Name": "n1", "Status": status, "SchedulingEligibility": elig,
            "Drain": drain}


def allocs(*statuses):
    return [{"JobID": f"pu-x-{i}", "ClientStatus": s}
            for i, s in enumerate(statuses)]


# (что проверяем, узел, аллокации, можно ли забыть)
CASES = [
    ("уведён и пуст", node(), allocs(), True),
    ("уведён, остались завершённые", node(), allocs("complete", "failed"), True),
    # Главное: живой папет на узле — отказ. Иначе узел исчезает из ростера, а
    # claude в нём продолжает работать, и мастер о нём больше не узнает.
    ("на узле работает папет", node(), allocs("running"), False),
    ("работает один из многих", node(), allocs("complete", "running"), False),
    ("запускается — тоже работа", node(), allocs("pending"), False),
    # Узел, открытый для планирования, забывать нельзя: планировщик поставит
    # на него папета между проверкой и сносом.
    ("узел ещё принимает папетов", node(elig="eligible"), allocs(), False),
    # Мёртвый узел забывают как раз затем, чтобы он не мозолил ростер.
    ("узел мёртв", node(status="down", elig="eligible"), allocs(), True),
    ("узел мёртв, но в нём числится папет",
     node(status="down"), allocs("running"), False),
]


def main():
    bad = 0
    cases = 0
    for what, n, a, want in CASES:
        cases += 1
        why = nomad.forget_refusal(n, a)
        if (why is None) != want:
            bad += 1
            print(f"FAILED  {what}: "
                  f"{'разрешил' if why is None else 'отказал: ' + why}, "
                  f"ждали {'разрешения' if want else 'отказа'}")
    # Отказ обязан называть причину: «нельзя» без причины заставляет лезть в
    # Nomad руками, а руками тут и ошибаются.
    cases += 1
    why = nomad.forget_refusal(node(), allocs("running"))
    if not why or "pu-x-0" not in why:
        bad += 1
        print(f"FAILED  отказ должен называть, кто мешает: {why!r}")

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
