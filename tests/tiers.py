#!/usr/bin/env python3
"""Политика ярусов LLM без пула: python3 tests/tiers.py

Ярусы -- «сильная модель, потом слабее, потом другой поставщик» -- целиком
наша политика (#286): в claude автоматического отката нет ни на модель ниже,
ни на другой ключ. Решение о ходе папета стоит на этой функции, поэтому она
чистая и проверяется здесь: разбор настройки MOP_LLM_TIERS и выбор хода по
таблице кредитов.

HYPOTHESIS: политики нет; лечение `quota` печатает одну модель из
MOP_FALLBACK_MODEL, а кредиты и ярусы провайдеров не знает никто.
SOLUTION: mop/common/tiers.py -- Tier, parse, choose -> Move; настройка
MOP_LLM_TIERS; `mop llm --tiers` показывает разобранные ярусы установки.
STATUS: FIXED — see #286
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from mop.common import tiers  # noqa: E402
from mop.common.domain import CredStatus  # noqa: E402

ACTIVE = CredStatus("active", percent=10, detail="ok")
WAIT = CredStatus("quota_wait", resets_at=1790425418, percent=100, detail="5h window full")
WAIT_LATER = CredStatus("quota_wait", resets_at=1790864188, percent=100, detail="weekly full")
DEAD = CredStatus("needs_login", detail="401")

TIERS = tiers.parse("claude:opus,claude:sonnet,glm")


def move(c, what, got, kind, **fields):
    """Ход ожидаемого вида и с ожидаемыми полями; причина -- непустая строка."""
    c.expect(f"{what}: kind", got.kind, kind)
    for k, v in fields.items():
        c.expect(f"{what}: {k}", getattr(got, k), v)
    c.check(f"{what}: has a reason", bool(got.reason), got)


def main():
    c = Checks()

    # ── разбор настройки ────────────────────────────────────────────────
    c.expect("parse: profile:model and bare profile", TIERS,
             [tiers.Tier("claude", "opus"), tiers.Tier("claude", "sonnet"),
              tiers.Tier("glm", None)])
    c.expect("parse: spaces and empty items are tolerated",
             tiers.parse(" claude:opus , ,glm "),
             [tiers.Tier("claude", "opus"), tiers.Tier("glm", None)])
    for bad in ("", "claude:", ":opus", "claude:opus:x", "плохо"):
        try:
            tiers.parse(bad)
            c.fail(f"parse({bad!r}) must refuse")
        except ValueError:
            pass
    try:
        tiers.parse("claude:opus,claude:opus")
        c.fail("parse must refuse a duplicate tier")
    except ValueError:
        pass

    # ── выбор хода ──────────────────────────────────────────────────────
    cur = ("claude", "opus", "anton")
    # Свой кредит жив -- стоим, даже если есть другие.
    move(c, "current active -> stay",
         tiers.choose(cur, {"anton": ("claude", ACTIVE), "ivan": ("claude", ACTIVE)},
                      TIERS, busy=True), "stay")
    # Свой кончился, у того же провайдера есть живой -- горячая подмена.
    move(c, "same profile active -> swap_cred",
         tiers.choose(cur, {"anton": ("claude", WAIT), "ivan": ("claude", ACTIVE)},
                      TIERS, busy=True), "swap_cred", cred="ivan", profile="claude")
    # Подмена и под занятым папетом: она горячая.
    move(c, "swap_cred is allowed when busy",
         tiers.choose(cur, {"anton": ("claude", WAIT), "ivan": ("claude", ACTIVE)},
                      TIERS, busy=True), "swap_cred", cred="ivan")
    # Модель ниже -- когда провал был про модель (вердикт `quota`/`error` на
    # конкретной модели), а кредит жив: у claude окно квоты общее на все
    # модели, и при исчерпанном кредите /model не спасает -- там swap_cred
    # или provider. Поэтому model_failed -- отдельный вход, а не вывод из
    # статуса кредита.
    move(c, "current active but the model failed -> model (weaker tier, same profile)",
         tiers.choose(cur, {"anton": ("claude", ACTIVE)}, TIERS, busy=True,
                      model_failed=True), "model", model="sonnet", profile="claude")
    # Ниже по ярусам у того же профиля ничего -- и модель на самом нижнем
    # ярусе профиля: следующий поставщик.
    move(c, "model failed on the last model of the profile -> provider",
         tiers.choose(("claude", "sonnet", "anton"), {"anton": ("claude", ACTIVE),
                                                      "z": ("glm", ACTIVE)},
                      TIERS, busy=False, model_failed=True), "provider",
         profile="glm", cred="z")
    # Свой кончился, у того же провайдера живых нет, у другого есть, папет
    # свободен -- переход провайдера.
    move(c, "other provider active, free -> provider",
         tiers.choose(cur, {"anton": ("claude", WAIT), "z": ("glm", ACTIVE)},
                      TIERS, busy=False), "provider", profile="glm", cred="z")
    # То же под занятым -- ждать: переход провайдера -- рестарт claude.
    got = tiers.choose(cur, {"anton": ("claude", WAIT), "z": ("glm", ACTIVE)},
                       TIERS, busy=True)
    move(c, "other provider active, busy -> wait", got, "wait")
    c.check("wait because of a restart names the puppet as busy",
            "busy" in got.reason, got.reason)
    # Живых нет нигде -- ждать до ближайшего сброса.
    got = tiers.choose(cur, {"anton": ("claude", WAIT_LATER), "z": ("glm", WAIT)},
                       TIERS, busy=False)
    move(c, "nothing active -> wait", got, "wait", resets_at=1790425418)
    # Мёртвые ключи ждать не дают: только ручная авторизация.
    got = tiers.choose(cur, {"anton": ("claude", DEAD)}, TIERS, busy=False)
    move(c, "only a dead credential -> wait without a reset", got, "wait", resets_at=None)
    c.check("a dead credential asks for a login", "login" in got.reason, got.reason)
    # Кредит с профилем не из ярусов не выбирается.
    move(c, "a credential of a profile outside the tiers is ignored",
         tiers.choose(cur, {"anton": ("claude", WAIT), "x": ("ollama", ACTIVE)},
                      TIERS, busy=False), "wait")
    # Детерминизм: два живых -- по имени.
    a = tiers.choose(cur, {"anton": ("claude", WAIT), "ivan": ("claude", ACTIVE),
                           "boris": ("claude", ACTIVE)}, TIERS, busy=True)
    b = tiers.choose(cur, {"boris": ("claude", ACTIVE), "anton": ("claude", WAIT),
                           "ivan": ("claude", ACTIVE)}, TIERS, busy=True)
    c.expect("deterministic: same answer regardless of dict order", a, b)
    c.expect("deterministic: ties by name", a.cred, "boris")
    # Незнакомый текущий профиль -- не отказ: политика всё равно ищет ход.
    move(c, "current profile outside the tiers -> provider when one is active",
         tiers.choose(("ollama", None, "x"), {"x": ("ollama", WAIT), "z": ("glm", ACTIVE)},
                      TIERS, busy=False), "provider", profile="glm")

    return c.report("tiers")


if __name__ == "__main__":
    sys.exit(main())
