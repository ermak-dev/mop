#!/usr/bin/env python3
"""Владелец задания без пула: python3 tests/lease.py

Два мастера одного проекта: первый отправил свободному папету тикет, второй
видит того же папета `free` — ветки ещё нет — и шлёт свой. В клоне два
тикета (#161). Вердикт «можно ли слать и брать ли аренду» — чистая функция
над записью владельца и фактами клона, и проверяется здесь.

HYPOTHESIS: занятость видна только по клону (ветка, несохранённое), а в окне
между диспатчем и первой веткой папет выглядит свободным, и кто его ведёт,
не записано нигде.
SOLUTION: запись `<логин>\\t<время>` в `.git/mop-owner` клона; чужая аренда
держит, пока в клоне работа или пока она моложе окна диспатча.
RESULT: 22/22; связка агента прогнана на временном клоне (гонка двух send
-- проходит один).
STATUS: FIXED — see #161
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import lease  # noqa: E402
from mop.domain import Owner  # noqa: E402

NOW = 1_000_000
CLEAN = {"cur": "master", "def": "master", "dirty": 0, "ahead": 0}
ON_BRANCH = dict(CLEAN, cur="bug/118-x")
DIRTY = dict(CLEAN, dirty=3)
AHEAD = dict(CLEAN, ahead=1)


def rec(user, age):
    return Owner(user, NOW - age)


def main():
    cases = bad = 0

    def check(what, got, want):
        nonlocal cases, bad
        cases += 1
        if got != want:
            bad += 1
            print(f"FAILED  {what}: got {got!r}, want {want!r}")

    def act(owner, me, clone, force=False):
        return lease.verdict(owner, me, clone, NOW, force=force)[0]

    # Запись: туда и обратно, мусор -- не владелец.
    # Строка файла -- Owner.render/parse (#204).
    check("render/parse", Owner.parse(Owner("anton", NOW).render()), Owner("anton", NOW))
    for junk in ("", "anton", "anton\tnot-a-time", "\t123", None):
        check(f"parse junk {junk!r}", Owner.parse(junk), None)

    # Отправитель не назвался (папет соседу) -- аренда не трогается.
    check("anonymous sender passes", act(rec("olga", 5), None, ON_BRANCH), "pass")
    # Ничей -- берём; свой -- берём (продлеваем).
    check("nobody's is taken", act(None, "anton", CLEAN), "take")
    check("own is refreshed", act(rec("anton", 5), "anton", DIRTY), "take")

    # Чужой: держит, пока в клоне работа...
    for what, clone in (("branch", ON_BRANCH), ("dirty", DIRTY), ("ahead", AHEAD)):
        check(f"foreign with work ({what}) refuses", act(rec("olga", 9999), "anton", clone), "refuse")
    # ...и пока аренда моложе окна: папет ещё не завёл ветку.
    check("foreign fresh on a clean clone refuses",
          act(rec("olga", lease.WINDOW - 1), "anton", CLEAN), "refuse")
    # Окно прошло, работы нет -- аренда истекла.
    check("foreign stale on a clean clone is taken",
          act(rec("olga", lease.WINDOW + 1), "anton", CLEAN), "take")
    # Клон неизвестен -- не значит «пусто».
    check("unknown clone holds", act(rec("olga", 9999), "anton", None), "refuse")
    # force забирает и называет, у кого.
    got = lease.verdict(rec("olga", 5), "anton", DIRTY, NOW, force=True)
    check("force takes", got[0], "take")
    check("force names the displaced", "olga" in (got[1] or ""), True)
    why = lease.verdict(rec("olga", 60), "anton", DIRTY, NOW)[1] or ""
    check("refusal names the owner and force", "olga" in why and "force" in why, True)

    # Ростер показывает только живую аренду.
    check("live: foreign with work", lease.live(rec("olga", 9999), ON_BRANCH, NOW), True)
    check("live: fresh on clean", lease.live(rec("olga", 5), CLEAN, NOW), True)
    check("not live: stale on clean", lease.live(rec("olga", lease.WINDOW + 1), CLEAN, NOW), False)
    check("not live: no record", lease.live(None, DIRTY, NOW), False)

    # ── ворота владения (#40) ─────────────────────────────────────────
    # HYPOTHESIS: владельца сверяет только send; slash/type, wipe, restart,
    # update, delete, recycle пускают любого мастера проекта к папету,
    # которого ведёт другой, и убивают его работу посреди тикета.
    # SOLUTION: одна чистая функция lease.may_touch на все изменяющие
    # действия; verdict send'а -- поверх неё, и решение одно.
    # STATUS: FIXED — see #40
    fn = getattr(lease, "may_touch", None)
    if fn is None:
        check("lease.may_touch exists", False, True)
    else:
        def touch(owner, me, clone, force=False, operator=False):
            return fn(owner, me, clone, NOW, force=force, operator=operator)

        check("touch: nobody's", touch(None, "anton", CLEAN)[0], True)
        check("touch: nobody's, anonymous", touch(None, None, DIRTY)[0], True)
        check("touch: own with work", touch(rec("anton", 9999), "anton", DIRTY)[0], True)
        for what, clone in (("branch", ON_BRANCH), ("dirty", DIRTY), ("ahead", AHEAD),
                            ("unknown clone", None)):
            ok, why = touch(rec("olga", 9999), "anton", clone)
            check(f"touch: foreign with work ({what}) refused", ok, False)
            check(f"touch: refusal names the owner ({what})",
                  "olga" in (why or "") and "force" in (why or ""), True)
        check("touch: foreign inside the dispatch window refused",
              touch(rec("olga", lease.WINDOW - 1), "anton", CLEAN)[0], False)
        check("touch: foreign expired on a clean clone",
              touch(rec("olga", lease.WINDOW + 1), "anton", CLEAN), (True, None))
        # Безымянный вызывающий к чужой живой аренде -- отказ: иначе ворота
        # обходятся тем, что не назваться.
        check("touch: anonymous against a live lease refused",
              touch(rec("olga", 5), None, DIRTY)[0], False)
        ok, why = touch(rec("olga", 5), "anton", DIRTY, force=True)
        check("touch: force passes", ok, True)
        check("touch: force names whom", "olga" in (why or ""), True)
        # Оператор (gc, doctor, wipe) не упирается никогда.
        check("touch: operator passes a foreign lease with work",
              touch(rec("olga", 5), "anton", DIRTY, operator=True)[0], True)
        check("touch: operator passes anonymously",
              touch(rec("olga", 5), None, DIRTY, operator=True)[0], True)

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
