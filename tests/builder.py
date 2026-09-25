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
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.server import builder  # noqa: E402


def main():
    failed = []
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
        got = builder.step_of(line)
        if got != want:
            failed.append(f"step_of({line!r}) -> {got!r}, wanted {want!r}")

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
        got = builder.needs_build(mode, serving, project)
        if got != want:
            failed.append(f"needs_build({mode!r}, {serving}, {project!r}) -> {got}, wanted {want}")
    try:
        builder.needs_build("fresh", {"a": []}, "rugent")
        failed.append("an unknown mode must be refused")
    except ValueError as e:
        if "fresh" not in str(e):
            failed.append(f"the refusal must name the mode: {e}")

    # HYPOTHESIS (#140): клиент видел только имя задачи; строки ansible
    # доходили лишь хвостом при отказе. SOLUTION: Batch копит строки и
    # отдаёт пачкой -- по времени (не publish на строку) или по размеру
    # (сообщение шины конечно); длинная строка режется.
    b = builder.Batch(interval=0.5, most=3, width=10)
    if b.add("a", now=0.0) is not None or b.add("b", now=0.2) is not None:
        failed.append("lines within the interval must wait for the batch")
    got = b.add("c", now=0.6)
    if got != ["a", "b", "c"]:
        failed.append(f"a line past the interval must send the batch: {got}")
    b.add("d", now=0.7)
    b.add("e", now=0.8)
    got = b.add("f", now=0.9)
    if got != ["d", "e", "f"]:
        failed.append(f"a full batch must go before the interval: {got}")
    # Тишина после строки (долгая задача): пачку забирает таймер, а не
    # следующая строка, которой может не быть минутами.
    b.add("g", now=1.0)
    if b.due(now=1.2) is not None:
        failed.append("the timer must not take a batch before the interval")
    if b.due(now=1.6) != ["g"]:
        failed.append("the timer must take a batch past the interval")
    if b.due(now=5.0) is not None or b.take() is not None:
        failed.append("an empty batch is None, not []")
    b.add("x" * 25, now=6.0)
    got = b.take()
    if got != ["x" * 10 + "…"]:
        failed.append(f"a long line must be cut to width: {got}")
    # STATUS: FIXED — see #140

    # HYPOTHESIS (#142): любой отказ сборщика шёл через nomad.describe_error
    # и с именем проекта, а клиент приписывал имя ещё раз: «rudesktop:
    # rudesktop: Nomad connection error: cannot read rudesktop: Host key
    # verification failed». SOLUTION: failure() -- RuntimeError (манифест,
    # занятые тела) как есть, Nomad -- через describe_error; имя ставит клиент.
    from mop.server import nomad
    got = builder.failure(RuntimeError("cannot read rudesktop: Host key verification failed."))
    if got != "cannot read rudesktop: Host key verification failed.":
        failed.append(f"a manifest refusal must go as it is: {got!r}")
    got = builder.failure(nomad.ApiError("403 permission denied"))
    if got != nomad.describe_error(nomad.ApiError("403 permission denied")):
        failed.append(f"a Nomad refusal must be named as Nomad's: {got!r}")
    import requests
    got = builder.failure(requests.exceptions.ConnectionError("refused"))
    if got != "Nomad connection error: refused":
        failed.append(f"an unreachable Nomad must be named as such: {got!r}")
    got = builder.failure(KeyError("NodeName"))
    if "Nomad" in got or "NodeName" not in got:
        failed.append(f"any other error must not pose as Nomad's: {got!r}")
    # STATUS: FIXED — see #142

    print("\n".join(f"FAIL {l}" for l in failed) if failed else "", end="\n" if failed else "")
    print("builder: FAILED" if failed else "builder: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
