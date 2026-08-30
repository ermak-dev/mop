#!/usr/bin/env python3
"""Проверка реестра LLM-профилей без пула: python3 tests/llm.py

Реестр — чистая функция над каталогом плагинов, и ошибка контракта обязана
находиться здесь, у мастера, а не на узле: KEY уезжает в sed-шаблон врапера,
кривое имя там молча совпадёт нигде, и слейв умрёт с «нет ключа» вдали от
причины.

Это не фреймворк и не прогон всего проекта: остальное по-прежнему добывается
на живом пуле.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import llm  # noqa: E402


def plugin(**attrs):
    """Модуль-плагин с заданными атрибутами; остальное берёт контрактом."""
    mod = types.ModuleType("fake")
    mod.__dict__.update(attrs)
    return mod


DEF = {"key": None, "auth_var": "ANTHROPIC_AUTH_TOKEN", "env": {}, "doc": ""}

CASES = [
    # (что проверяем, модуль, ожидание: контракт | None = громкий отказ)
    # ENV обязателен явно, даже пустой: контракт не угадывает намерений.
    ("минимум: пустой ENV", plugin(ENV={}), DEF),
    ("ключ и auth_var прочитаны", plugin(ENV={}, KEY="K", AUTH_VAR="VAR"),
     {**DEF, "key": "K", "auth_var": "VAR"}),
    ("описание — первая строка docstring", plugin(ENV={}, __doc__="один\nдва"),
     {**DEF, "doc": "один"}),
    ("ENV не объявлен", plugin(), None),
    ("ENV не словарь", plugin(ENV="A=b"), None),
    ("ENV с нестрочным значением", plugin(ENV={"A": 5}), None),
    ("KEY не имя переменной", plugin(KEY="плохое-имя"), None),
    ("AUTH_VAR не имя переменной", plugin(AUTH_VAR="1bad"), None),
]


def main():
    bad = 0
    cases = 0
    for what, mod, want in CASES:
        cases += 1
        try:
            got = llm.contract("fake", mod)
        except RuntimeError:
            got = None
        if got != want:
            bad += 1
            print(f"ПРОВАЛ  {what}\n  ждали:  {want!r}\n  вышло: {got!r}")

    for name in ("claude", "glm"):
        cases += 1
        if not isinstance(llm.get(name), dict):
            bad += 1
            print(f"ПРОВАЛ  реестр не нашёл профиль {name}")
    cases += 1
    if llm.get("no-such") is not None:
        bad += 1
        print("ПРОВАЛ  get() чужого имени обязан вернуть None")
    cases += 1
    try:
        llm.require("no-such")
        bad += 1
        print("ПРОВАЛ  require() чужого имени обязан отказаться")
    except RuntimeError:
        pass

    print(f"{cases - bad}/{cases} сошлось")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
