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

from mop.common import lease  # noqa: E402
from mop.common.domain import CloneFacts, Owner  # noqa: E402

NOW = 1_000_000
CLEAN = {"cur": "master", "def": "master", "dirty": 0, "ahead": 0}
ON_BRANCH = dict(CLEAN, cur="bug/118-x")
DIRTY = dict(CLEAN, dirty=3)
AHEAD = dict(CLEAN, ahead=1)

# Факты клона в том виде, в каком их шлёт агент по шине (#266): ключи
# cur/def/... -- провод, их держат агенты обеих версий во время раската.
WIRE = {"cur": "bug/118-x", "def": "master", "home": "master",
        "origin": "git@git.example.dev:someone/mop.git",
        "dirty": 2, "ahead": 1, "owner": {"user": "olga", "at": NOW}}


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
        return lease.verdict(owner, me, CloneFacts.from_dict(clone), NOW, force=force)[0]

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
    for what, clone in (("dirty", DIRTY), ("ahead", AHEAD)):
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
    got = lease.verdict(rec("olga", 5), "anton", CloneFacts.from_dict(DIRTY), NOW, force=True)
    check("force takes", got[0], "take")
    check("force names the displaced", "olga" in (got[1] or ""), True)
    why = lease.verdict(rec("olga", 60), "anton", CloneFacts.from_dict(DIRTY), NOW)[1] or ""
    check("refusal names the owner and force", "olga" in why and "force" in why, True)

    # Ростер показывает только живую аренду.
    def live(owner, clone):
        return lease.live(owner, CloneFacts.from_dict(clone), NOW)
    check("live: foreign with work", live(rec("olga", 9999), DIRTY), True)
    check("live: fresh on clean", live(rec("olga", 5), CLEAN), True)
    check("not live: stale on clean", live(rec("olga", lease.WINDOW + 1), CLEAN), False)
    check("not live: no record", live(None, DIRTY), False)

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
            return fn(owner, me, CloneFacts.from_dict(clone), NOW, force=force,
                      operator=operator)

        check("touch: nobody's", touch(None, "anton", CLEAN)[0], True)
        check("touch: nobody's, anonymous", touch(None, None, DIRTY)[0], True)
        check("touch: own with work", touch(rec("anton", 9999), "anton", DIRTY)[0], True)
        for what, clone in (("dirty", DIRTY), ("ahead", AHEAD), ("unknown clone", None)):
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

    # ── ветка не по умолчанию -- не работа (#266) ────────────────────
    # HYPOTHESIS: «в клоне работа» записано трижды, и копии расходятся:
    # lease.holds_work считал работой ветку не по умолчанию, а state и
    # classify_junk -- только dirty/ahead. С #256 папет намеренно стоит на
    # ветке своего мастера (mop.branch), и для аренды такой папет держит
    # работу вечно: другой мастер не берёт его без --force, хотя ростер
    # зовёт его free.
    # SOLUTION: domain.CloneFacts и один предикат domain.holds_work =
    # dirty или ahead (клон неизвестен -- держит); ветку свежего диспатча
    # по-прежнему защищает окно lease.WINDOW.
    # STATUS: FIXED — see #266
    # С #272 «ветка мастера» -- это дом клона (home): папет #256 стоит на нём.
    ON_MASTERS = dict(ON_BRANCH, home=ON_BRANCH["cur"])
    stale = rec("olga", lease.WINDOW + 1)
    check("#266: clean on the master's branch with a stale owner may be touched",
          lease.may_touch(stale, "anton", CloneFacts.from_dict(ON_MASTERS), NOW), (True, None))
    check("#266: clean on the master's branch with a stale owner is not live",
          lease.live(stale, CloneFacts.from_dict(ON_MASTERS), NOW), False)
    for what, clone in (("dirty", dict(ON_MASTERS, dirty=1)), ("ahead", dict(ON_MASTERS, ahead=1))):
        check(f"#266: master's branch with work ({what}) still refused",
              lease.may_touch(stale, "anton", CloneFacts.from_dict(clone), NOW)[0], False)
    check("#266: clean master's branch inside the dispatch window still refused",
          lease.may_touch(rec("olga", lease.WINDOW - 1), "anton",
                          CloneFacts.from_dict(ON_MASTERS), NOW)[0], False)

    # ── дом клона (#272) ──────────────────────────────────────────────
    # HYPOTHESIS: после #266 папет, закончивший тикет (запушено, чисто, ждёт
    # приёма отчёта), через lease.WINDOW без send'ов берёт другой мастер без
    # --force: ветку тикета правило больше не видит.
    # SOLUTION: у клона есть дом -- ветка мастера из меты джоба, иначе
    # ветка по умолчанию; стадия клона пишет её в `git config mop.home`,
    # агент отдаёт ключом home. Чистый клон не на доме держит тикет. Старый
    # агент ключа не шлёт -- дом его клона ветка по умолчанию.
    # STATUS: FIXED — see #272
    waiting = {"cur": "bug/272-x", "def": "master", "home": "master", "dirty": 0, "ahead": 0}
    ok, why = lease.may_touch(stale, "anton", CloneFacts.from_dict(waiting), NOW)
    check("#272: a puppet waiting for accept refuses another master", ok, False)
    check("#272: the refusal names the ticket branch and the owner",
          "bug/272-x" in (why or "") and "olga" in (why or ""), True)
    home = {"cur": "swarm", "def": "master", "home": "swarm", "dirty": 0, "ahead": 0}
    check("#272: clean on its home (the master's branch) with a stale owner is takeable",
          lease.may_touch(stale, "anton", CloneFacts.from_dict(home), NOW), (True, None))
    check("#272: off its home even on the default branch holds",
          lease.may_touch(stale, "anton", CloneFacts.from_dict(dict(home, cur="master")),
                          NOW)[0], False)
    old = {"cur": "bug/272-x", "def": "master", "dirty": 0, "ahead": 0}
    check("#272: old agent (no home) off the default branch holds",
          lease.may_touch(stale, "anton", CloneFacts.from_dict(old), NOW)[0], False)
    check("#272: old agent (no home) on the default branch is takeable",
          lease.may_touch(stale, "anton", CloneFacts.from_dict(dict(old, cur="master")),
                          NOW), (True, None))
    check("#272: no home recorded -- home is the default branch",
          CloneFacts.from_dict(dict(old, home=None)).home_branch, "master")
    fresh = lease.may_touch(rec("olga", 60), "anton", CloneFacts.from_dict(home), NOW)[1] or ""
    check("#272: inside the window the refusal says nothing is committed yet",
          "nothing committed yet" in fresh and "no branch" not in fresh, True)

    # Провод не двигается: факты агента туда и обратно -- байт в байт, и
    # ключи в том же порядке (JSON снимка их сравнивает строкой).
    for what, wire in (("full", WIRE), ("no owner", dict(WIRE, owner=None)),
                       ("detached, no default", dict(WIRE, cur="(detached)", **{"def": None})),
                       ("no home recorded (old agent)",
                        {k: v for k, v in WIRE.items() if k != "home"})):
        back = CloneFacts.from_dict(wire).to_dict()
        check(f"#266: CloneFacts round-trips the agent's dict ({what})",
              (back, list(back)), (wire, list(wire)))
    check("#266: no clone data is no CloneFacts", CloneFacts.from_dict(None), None)

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
