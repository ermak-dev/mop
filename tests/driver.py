#!/usr/bin/env python3
"""Проверка реестра драйверов узла без пула: python3 tests/driver.py

Драйвер отвечает на третий вопрос системы — В ЧЁМ живёт папет (docs/DRIVER.md).
Реестр — чистая функция над каталогом плагинов, и нарушение контракта обязано
находиться здесь, а не на узле: агент зовёт глаголы драйвера из петли, и
отсутствующий `argv` там прочитается как «узел молчит», а не как «драйвер
кривой».

Проверяется ровно то, что проверяемо без пула: контракт реестра, чистые
функции драйвера host (префиксы команд) и проверка имени, из которого драйвер
собирает шелл. Всё остальное — создание тела, ssh в него, лимиты — добывается
на живом пуле.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import driver  # noqa: E402


def plugin(**attrs):
    """Модуль-плагин с заданными атрибутами; остальное берёт контрактом."""
    mod = types.ModuleType("fake")
    for verb in driver.VERBS:
        mod.__dict__[verb] = lambda *a, **k: None
    mod.SESSION_PY = "/opt/mop/mop/session.py"
    mod.__dict__.update(attrs)
    return mod


# ── контракт реестра ─────────────────────────────────────────────────────
CONTRACT = [
    # (что проверяем, модуль, ок?)
    ("complete module", plugin(), True),
    ("description — first line of docstring", plugin(__doc__="one\ntwo"), True),
    ("no argv", plugin(argv=None), False),
    ("no ensure", plugin(ensure=None), False),
    ("no destroy", plugin(destroy=None), False),
    ("no bodies", plugin(bodies=None), False),
    ("no capacity", plugin(capacity=None), False),
    ("no push", plugin(push=None), False),
    ("no projects_dir", plugin(projects_dir=None), False),
    ("no attach_argv", plugin(attach_argv=None), False),
    ("no repair_argv", plugin(repair_argv=None), False),
    ("a verb that is not callable", plugin(argv="ssh"), False),
    # SESSION_PY уезжает в шелл внутри тела. Пустое значение там молча
    # соберётся в `python3  probe <clone>` — python прочитает probe как файл.
    ("no SESSION_PY", plugin(SESSION_PY=None), False),
    ("empty SESSION_PY", plugin(SESSION_PY=""), False),
    ("SESSION_PY is not a path", plugin(SESSION_PY="session.py"), False),
]

# Имя папета склеивается в шелл — и у host, и у драйвера контейнеров. Проверка
# имени поэтому общая, в самом реестре: два списка разъехались бы молча.
NAMES = [
    ("pu-mop-1", True),
    ("pu-some-project-12", True),
    ("mop-1", False),            # без префикса — не папет
    ("pu-mop-1/../etc", False),
    ("pu-mop 1", False),
    ("pu-mop-1;rm -rf /", False),
    ("pu-$(id)-1", False),
    ("", False),
]

# Драйвер host: тело равно узлу, поэтому префиксы пустые, а человек входит
# прямо в tmux-сервер папета. На этом стоит `mop attach`.
HOST_ARGV = [
    ("argv is empty: the body is the node", "argv", []),
    ("repair path is the same as the main one", "repair_argv", []),
    ("a human attaches to the puppet's own tmux server", "attach_argv",
     ["tmux", "-L", "pu-mop-1", "attach", "-t", "pu-mop-1"]),
]


def main():
    bad = 0
    cases = 0

    for what, mod, ok in CONTRACT:
        cases += 1
        try:
            got = driver.contract("fake", mod)
            refused = False
        except RuntimeError:
            got, refused = None, True
        if refused == ok:
            bad += 1
            print(f"FAILED  contract: {what} — "
                  f"{'refused' if refused else 'accepted'}, wanted the opposite")
        elif ok and got.get("doc") and not isinstance(got["doc"], str):
            bad += 1
            print(f"FAILED  contract: {what} — doc is not a string")

    cases += 1
    if driver.contract("fake", plugin(__doc__="one\ntwo"))["doc"] != "one":
        bad += 1
        print("FAILED  contract: doc must be the first line of the docstring")

    for name, ok in NAMES:
        cases += 1
        if driver.valid_name(name) != ok:
            bad += 1
            print(f"FAILED  valid_name({name!r}) must be {ok}")

    cases += 1
    if not isinstance(driver.get("host"), dict):
        bad += 1
        print("FAILED  registry didn't find the host driver")
    cases += 1
    if driver.get("no-such") is not None:
        bad += 1
        print("FAILED  get() of an unknown name must return None")
    cases += 1
    try:
        driver.require("no-such")
        bad += 1
        print("FAILED  require() of an unknown name must refuse")
    except RuntimeError:
        pass

    # Драйвер УЗЛА, а не папета: значение приезжает в окружение агента юнитом,
    # и подставить его запросом с шины нельзя. Дефолт — host: узел, ничего про
    # драйвер не знающий, обязан вести себя как раньше.
    host = driver.module("host")
    for what, verb, want in HOST_ARGV:
        cases += 1
        got = getattr(host, verb)("pu-mop-1")
        if got != want:
            bad += 1
            print(f"FAILED  host.{verb}: {what}\n  wanted: {want!r}\n  got: {got!r}")

    cases += 1
    saved = os.environ.pop("MOP_DRIVER", None)
    try:
        if driver.current_name() != "host":
            bad += 1
            print("FAILED  a node that says nothing about a driver must be host")
        os.environ["MOP_DRIVER"] = "host"
        if driver.current() is not host:
            bad += 1
            print("FAILED  current() must follow MOP_DRIVER")
    finally:
        os.environ.pop("MOP_DRIVER", None)
        if saved is not None:
            os.environ["MOP_DRIVER"] = saved

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
