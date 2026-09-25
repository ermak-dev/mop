#!/usr/bin/env python3
"""Проверка реестра LLM-профилей без пула: python3 tests/llm.py

Реестр — чистая функция над каталогом плагинов, и ошибка контракта обязана
находиться здесь, у мастера, а не на узле: KEY уезжает в sed-шаблон врапера,
кривое имя там молча совпадёт нигде, и папет умрёт с «нет ключа» вдали от
причины.

Это не фреймворк и не прогон всего проекта: остальное по-прежнему добывается
на живом пуле.
"""
import os
import sys
import types

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import config, llm  # noqa: E402


def plugin(**attrs):
    """Модуль-плагин с заданными атрибутами; остальное берёт контрактом."""
    mod = types.ModuleType("fake")
    mod.__dict__.update(attrs)
    return mod


DEF = {"key": None, "auth_var": "ANTHROPIC_AUTH_TOKEN", "env": {}, "doc": ""}

CASES = [
    # (что проверяем, модуль, ожидание: контракт | None = громкий отказ)
    # ENV обязателен явно, даже пустой: контракт не угадывает намерений.
    ("minimum: empty ENV", plugin(ENV={}), DEF),
    ("key and auth_var read", plugin(ENV={}, KEY="K", AUTH_VAR="VAR"),
     {**DEF, "key": "K", "auth_var": "VAR"}),
    ("description — first line of docstring", plugin(ENV={}, __doc__="one\ntwo"),
     {**DEF, "doc": "one"}),
    ("ENV not declared", plugin(), None),
    ("ENV not a dict", plugin(ENV="A=b"), None),
    ("ENV with a non-string value", plugin(ENV={"A": 5}), None),
    ("KEY not a variable name", plugin(KEY="плохое-имя"), None),
    ("AUTH_VAR not a variable name", plugin(AUTH_VAR="1bad"), None),
]


def main():
    c = Checks()
    for what, mod, want in CASES:
        try:
            got = llm.contract("fake", mod)
        except RuntimeError:
            got = None
        c.expect(what, got, want)

    for name in ("claude", "glm"):
        c.check(f"registry must find profile {name}", isinstance(llm.get(name), dict))
    c.expect("get() of an unknown name must return None", llm.get("no-such"), None)
    try:
        llm.require("no-such")
        refused = False
    except RuntimeError:
        refused = True
    c.check("require() of an unknown name must refuse", refused)

    # Правило выбора профиля (#147): одно на все места, где его раньше
    # набирали руками. Умолчание берём из настройки, а не литералом: .env
    # инсталляции вправе его сменить.
    # STATUS: FIXED — see #147
    default = config.get("MOP_DEFAULT_LLM")
    other = next(p for p in llm.profiles() if p != default)
    resolve, of_meta = llm.resolve, llm.of_meta
    RULE = [
        ("explicit given -> explicit", lambda: resolve(other, default), other),
        ("no explicit, old exists -> old", lambda: resolve(None, other), other),
        ("no explicit, old deleted from registry -> default",
         lambda: resolve(None, "no-such"), default),
        ("nothing -> default", lambda: resolve(), default),
        ("meta without llm -> default", lambda: of_meta({}), default),
        ("meta with llm -> its profile, even a deleted one",
         lambda: of_meta({"llm": "no-such"}), "no-such"),
        ("no meta at all -> default", lambda: of_meta(None), default),
        # Пустая строка: места расходятся сегодня, и рефакторинг обязан это
        # сохранить. `x or default` (явный профиль в job_spec/add/code/master,
        # image.clear) и откат update превращают "" в умолчание; `meta.get(
        # "llm", default)` (строка ростера, старый профиль в update, recycle)
        # отдаёт "" как есть. Спека в итоге всё равно умолчание: job_spec
        # снова пропускает профиль через resolve.
        ("empty explicit -> default (job_spec, add, code, master, image.clear)",
         lambda: resolve(""), default),
        ("empty old -> default (update fallback)",
         lambda: resolve(None, ""), default),
        ("meta llm empty -> empty (roster row, update's old, recycle)",
         lambda: of_meta({"llm": ""}), ""),
    ]
    for what, run, want in RULE:
        try:
            got = run()
        except Exception as e:
            got = f"{type(e).__name__}: {e}"
        c.expect(what, got, want)
    return c.report("llm")


if __name__ == "__main__":
    sys.exit(main())
