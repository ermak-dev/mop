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
from mop.common.llm import claude, glm  # noqa: E402


def plugin(**attrs):
    """Модуль-плагин с заданными атрибутами; остальное берёт контрактом."""
    mod = types.ModuleType("fake")
    mod.__dict__.update(attrs)
    return mod


# Контракт знает один хук, probe (#310): usage не звал никто, и он снят.
DEF = {"key": None, "auth_var": "ANTHROPIC_AUTH_TOKEN", "env": {}, "doc": "",
       "probe": None}

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
    # Пробы провайдера (#287) -- необязательны: плагин без них проходит
    # контракт как прежде, с ними -- контракт их называет; не функция -- отказ.
    ("probe absent -> None", plugin(ENV={}), DEF),
    # usage у плагина (#310) -- не хук: контракт его не читает и не несёт.
    ("a usage attribute is not a hook", plugin(ENV={}, usage=lambda k, s, e: {}), DEF),
    ("probe not callable", plugin(ENV={}, probe="yes"), None),
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

    # ── мост на прокси (#382): профиль перестаёт быть провайдером ─────────
    # HYPOTHESIS: единственный LLM-сервер установки -- прокси на контроллере
    # (#380), и разница профилей умерла: оба -- одинаковые shim'ы с общим
    # ключом и общим адресом из настройки, карта моделей -- сегодняшняя
    # стенда (glm), меняется только дорога. Пробы умирают: квоты видит
    # панель прокси.
    bridge = {"ANTHROPIC_BASE_URL": config.get("MOP_PROXY_URL"),
              "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3[1m]",
              "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3[1m]",
              "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5.3-flash",
              "API_TIMEOUT_MS": "3000000"}
    c.check("MOP_PROXY_URL is a non-empty setting (#382)",
            bool(config.get("MOP_PROXY_URL")), config.get("MOP_PROXY_URL"))
    for name, mod in (("claude", claude), ("glm", glm)):
        prof = llm.get(name)
        c.expect(f"{name}: KEY is the proxy key (#382)", prof["key"], "MOP_PROXY_KEY")
        c.expect(f"{name}: ENV leads to the proxy (#382)", prof["env"], bridge)
        c.check(f"{name}: no probe -- quotas live in the proxy panel (#382)",
                prof.get("probe") is None and not hasattr(mod, "probe"),
                getattr(prof, "probe", "present"))
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

    # Пробы и их фиксстуры сняты (#382): провайдеров больше нет, квоты видит
    # панель прокси. Контракт probe по-прежнему проверяется CASES -- хук
    # остаётся необязательной частью контракта до #390.
    got = llm.contract("fake", plugin(ENV={}, probe=lambda k: None, usage=lambda k, s, e: {}))
    c.check("contract names probe, and only probe (#310)",
            callable(got["probe"]) and "usage" not in got, got)
    return c.report("llm")


if __name__ == "__main__":
    sys.exit(main())
