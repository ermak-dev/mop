#!/usr/bin/env python3
"""Проверка сборщика образов без пула: python3 tests/builder.py

Сборщик (#123) — подписчик на сервере, который собирает образ проекта по
просьбе оператора и шлёт ход потоком. Чистое здесь — какой шаг показать по
строке вывода ansible и собирать ли вообще; сама сборка, поток по шине и
перезапуск тел проверяются только на живом пуле.
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.server import builder  # noqa: E402


def main():
    c = Checks()
    # HYPOTHESIS (#123): собрать образ можно было только на контроллере, и
    # вывод был полным логом ansible. SOLUTION: сборщик на сервере шлёт шаг --
    # имя задачи ansible. STATUS: FIXED — see #123
    for line, want in [
        ("TASK [body : Ensure the tools a puppet works with] ****************", "Ensure the tools a puppet works with"),
        ("TASK [Wait for sshd inside the body] *******", "Wait for sshd inside the body"),
        ("RUNNING HANDLER [web : restart mop-web] ****", None),
        ("ok: [hyper]", None),
        ("PLAY RECAP *****", None),
        ("", None),
    ]:
        c.expect(f"step_of({line!r})", builder.step_of(line), want)

    # Собирать ли. missing -- только если образа нет хотя бы на одном
    # контейнерном узле; узлов-контейнеров нет -- собирать нечего.
    cases = [
        ("missing", {"a": [], "b": ["rugent"]}, "rugent", True),
        ("missing", {"a": ["rugent"], "b": ["mop", "rugent"]}, "rugent", False),
        ("missing", {}, "rugent", False),
        ("update", {"a": ["rugent"]}, "rugent", True),
        ("rebuild", {"a": ["rugent"]}, "rugent", True),
        ("update", {}, "rugent", False),
    ]
    for mode, serving, project, want in cases:
        c.expect(f"needs_build({mode!r}, {serving}, {project!r})",
                 builder.needs_build(mode, serving, project), want)
    try:
        builder.needs_build("fresh", {"a": []}, "rugent")
        c.fail("an unknown mode must be refused")
    except ValueError as e:
        c.check("the refusal must name the mode", "fresh" in str(e), e)

    # HYPOTHESIS (#140): клиент видел только имя задачи; строки ansible
    # доходили лишь хвостом при отказе. SOLUTION: Batch копит строки и
    # отдаёт пачкой -- по времени (не publish на строку) или по размеру
    # (сообщение шины конечно); длинная строка режется.
    b = builder.Batch(interval=0.5, most=3, width=10)
    c.check("lines within the interval must wait for the batch",
            not (b.add("a", now=0.0) is not None or b.add("b", now=0.2) is not None))
    c.expect("a line past the interval must send the batch", b.add("c", now=0.6),
             ["a", "b", "c"])
    b.add("d", now=0.7)
    b.add("e", now=0.8)
    c.expect("a full batch must go before the interval", b.add("f", now=0.9), ["d", "e", "f"])
    # Тишина после строки (долгая задача): пачку забирает таймер, а не
    # следующая строка, которой может не быть минутами.
    b.add("g", now=1.0)
    c.expect("the timer must not take a batch before the interval", b.due(now=1.2), None)
    c.expect("the timer must take a batch past the interval", b.due(now=1.6), ["g"])
    c.check("an empty batch is None, not []",
            not (b.due(now=5.0) is not None or b.take() is not None))
    b.add("x" * 25, now=6.0)
    c.expect("a long line must be cut to width", b.take(), ["x" * 10 + "…"])
    # STATUS: FIXED — see #140

    # HYPOTHESIS (#142): любой отказ сборщика шёл через nomad.describe_error
    # и с именем проекта, а клиент приписывал имя ещё раз: «rudesktop:
    # rudesktop: Nomad connection error: cannot read rudesktop: Host key
    # verification failed». SOLUTION: failure() -- RuntimeError (манифест,
    # занятые тела) как есть, Nomad -- через describe_error; имя ставит клиент.
    from mop.server import nomad
    c.expect("a manifest refusal must go as it is",
             builder.failure(RuntimeError("cannot read rudesktop: Host key verification failed.")),
             "cannot read rudesktop: Host key verification failed.")
    c.expect("a Nomad refusal must be named as Nomad's",
             builder.failure(nomad.ApiError("403 permission denied")),
             nomad.describe_error(nomad.ApiError("403 permission denied")))
    import requests
    c.expect("an unreachable Nomad must be named as such",
             builder.failure(requests.exceptions.ConnectionError("refused")),
             "Nomad connection error: refused")
    got = builder.failure(KeyError("NodeName"))
    c.check("any other error must not pose as Nomad's",
            not ("Nomad" in got or "NodeName" not in got), repr(got))
    # STATUS: FIXED — see #142
    return c.report("builder")


if __name__ == "__main__":
    sys.exit(main())
