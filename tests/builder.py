#!/usr/bin/env python3
"""Проверка сборщика образов без пула: python3 tests/builder.py

Сборщик (#123) — подписчик на сервере, который собирает образ проекта по
просьбе оператора и шлёт ход потоком. Чистое здесь — какой шаг показать по
строке вывода ansible и собирать ли вообще; сама сборка, поток по шине и
перезапуск тел проверяются только на живом пуле.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import builder  # noqa: E402


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

    print("\n".join(f"FAIL {l}" for l in failed) if failed else "", end="\n" if failed else "")
    print("builder: FAILED" if failed else "builder: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
