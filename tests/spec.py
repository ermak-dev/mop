#!/usr/bin/env python3
"""Проверка спеки джоба без пула: python3 tests/spec.py

Спека собирается чистой функцией, а ошибка в ней стоит дороже почти всего
остального: ограничение размещения, промахнувшееся в одну сторону, делает
НЕПЛАНИРУЕМЫМ весь пул, а промахнувшееся в другую — не ловит ничего, и папет
садится на узел, который не умеет его обслужить. И то и другое видно только по
симптому: «queued без аллокации» либо «pending навсегда».

Это не фреймворк и не прогон всего проекта: остальное по-прежнему добывается
на живом пуле.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import puppets  # noqa: E402

ORIGIN = "git@git.example.dev:someone/mop.git"


def shard_constraint(spec):
    """Ограничение про шарды из спеки; None, если его там нет."""
    for c in spec["Job"].get("Constraints") or []:
        if c.get("LTarget") == "${meta.mop_shards}":
            return c
    return None


def serves(shard, meta_value):
    """Сядет ли папет шарда на узел, объявивший такое meta.mop_shards.

    Считаем ровно тем, что уедет в Nomad: RTarget ограничения как регулярное
    выражение над значением meta. Go RE2 и python re на этом классе выражений
    ведут себя одинаково.

    Спеку собираем от ORIGIN этого шарда, а не от имени: шард в системе
    определяется ровно одним способом — basename origin без .git."""
    c = shard_constraint(puppets.job_spec(
        f"pu-{shard}-1", f"git@git.example.dev:someone/{shard}.git"))
    assert c and c.get("Operand") == "regexp", f"no regexp constraint: {c!r}"
    return re.search(c["RTarget"], meta_value) is not None


# (шард, что узел объявил, сядет ли)
CASES = [
    # Узел общего назначения — тот, где тело равно узлу, — обслуживает любой
    # шард, в том числе заведённый ПОСЛЕ прогона deploy. Без этого первый же
    # `mop add` нового проекта вешал бы папета в queued навсегда.
    ("mop", "any", True),
    ("совсем-новый-шард", "any", True),

    ("mop", "mop", True),
    ("mop", "mop,rugent", True),
    ("mop", "rugent,mop", True),
    ("mop", "rugent,mop,cloudpub", True),

    # Узел, который этого шарда не умеет.
    ("mop", "rugent", False),
    ("mop", "", False),

    # Ловушка подстроки, и она обоюдная: без якорей `mop` совпал бы с `mop2`,
    # а `op` — с `mop`. Оба промаха тихие: папет уезжает на узел, где образа
    # его шарда нет, и висит pending, пока кто-нибудь не прочитает лог задачи
    # НА УЗЛЕ.
    ("mop", "mop2", False),
    ("mop", "not-mop", False),
    ("op", "mop", False),
    ("mop", "mopmop", False),

    # `any` — слово целиком, а не приставка: узел, объявивший `anything`,
    # обслуживает шард `anything`, и только его.
    ("mop", "anything", False),
    ("anything", "anything", True),

    # Имя шарда — basename репозитория, в нём бывают точка и дефис. Точка в
    # незаэкранированном выражении совпадает с чем угодно.
    ("my.proj", "my.proj", True),
    ("my.proj", "myXproj", False),
    ("my-proj", "my-proj", True),
]


def main():
    bad = 0
    cases = 0

    for shard, meta, want in CASES:
        cases += 1
        got = serves(shard, meta)
        if got != want:
            bad += 1
            print(f"FAILED  shard {shard!r} on a node announcing {meta!r}: "
                  f"got {got}, wanted {want}")

    # Ограничение обязано быть В КАЖДОЙ спеке: папет без него садится куда
    # угодно, и вся проверка становится украшением.
    cases += 1
    if shard_constraint(puppets.job_spec("pu-mop-1", ORIGIN)) is None:
        bad += 1
        print("FAILED  a job spec without the shard constraint schedules anywhere")

    # Шард берётся из ORIGIN, а не из имени: имя — производное, и разойтись
    # они могут только при ручной регистрации, где ошибка и опаснее всего.
    cases += 1
    c = shard_constraint(puppets.job_spec("pu-anything-7", ORIGIN))
    if not re.search(c["RTarget"], "mop"):
        bad += 1
        print("FAILED  the constraint must follow the origin's shard, not the name")

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
