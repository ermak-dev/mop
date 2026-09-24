#!/usr/bin/env python3
"""Проверка спеки джоба без пула: python3 tests/spec.py

Спека собирается чистой функцией, а ошибка в ней стоит дороже почти всего
остального: ограничение размещения, промахнувшееся в одну сторону, делает
непланируемым весь пул, а промахнувшееся в другую — не ловит ничего, и папет
садится на узел, который не умеет его обслужить. И то и другое видно только по
симптому: «queued без аллокации» либо «pending навсегда».

Это не фреймворк и не прогон всего проекта: остальное по-прежнему добывается
на живом пуле.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

# ── #152: слепок спеки ────────────────────────────────────────────────────────
# Спека зарегистрированного папета живёт в Nomad, и любое её расхождение с
# тем, что соберёт следующая версия кода, видно только по симптому:
# spec_is_stale метит весь пул, doctor перерегистрирует каждого папета на обеих
# установках. Поэтому перенос job_spec между модулями сверяется со слепком
# байт в байт, а не «по смыслу».
#
# Всё, что спека читает из установки, прибито окружением ДО импорта mop
# (окружение старше .env и node.env, см. mop/config.py): иначе слепок совпадал
# бы только на машине, где его сняли. Время — единственный недетерминизм
# (PU_CONTINUE) — заморожено тем же способом.
PINNED = {
    "MOP_HOME": "/home/pool",
    "MOP_USER": "pool",
    "MOP_PUPPET_MEM_MB": "4096",
    "MOP_MEM_MB": "8192",
    "MOP_PUPPET_SEED": ".env*,.providers",
    "MOP_PUPPET_PATH": "{HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin",
    "MOP_POOL_DC": "pool",
    "MOP_DEFAULT_LLM": "claude",
}
os.environ.update(PINNED)
time.time = lambda: 1_790_000_000.0
SNAPSHOT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "spec_snapshot.json")

from mop import spec  # noqa: E402

ORIGIN = "git@git.example.dev:someone/mop.git"


def project_constraint(rendered):
    """Ограничение про проекты из спеки; None, если его там нет. Литерал
    ключа здесь нарочно, а не spec.PROJECTS_TARGET: проверка читает то, что
    уедет в Nomad, а не то, что код о себе думает."""
    for c in rendered["Job"].get("Constraints") or []:
        if c.get("LTarget") == "${meta.mop_projects}":
            return c
    return None


def serves(project, meta_value):
    """Сядет ли папет проекта на узел, объявивший такое meta.mop_projects.

    Считаем ровно тем, что уедет в Nomad: RTarget ограничения как регулярное
    выражение над значением meta. Go RE2 и python re на этом классе выражений
    ведут себя одинаково.

    Спеку собираем от ORIGIN этого проекта, а не от имени: проект в системе
    определяется ровно одним способом — basename origin без .git."""
    c = project_constraint(spec.job_spec(
        f"pu-{project}-1", f"git@git.example.dev:someone/{project}.git"))
    assert c and c.get("Operand") == "regexp", f"no regexp constraint: {c!r}"
    return re.search(c["RTarget"], meta_value) is not None


# (проект, что узел объявил, сядет ли)
CASES = [
    # Узел общего назначения — тот, где тело равно узлу, — обслуживает любой
    # проект, в том числе заведённый после прогона deploy. Без этого первый же
    # `mop add` нового проекта вешал бы папета в queued навсегда.
    ("mop", "any", True),
    ("совсем-новый-проект", "any", True),

    ("mop", "mop", True),
    ("mop", "mop,rugent", True),
    ("mop", "rugent,mop", True),
    ("mop", "rugent,mop,cloudpub", True),

    # Узел, который этого проекта не умеет.
    ("mop", "rugent", False),
    ("mop", "", False),

    # Ловушка подстроки, и она обоюдная: без якорей `mop` совпал бы с `mop2`,
    # а `op` — с `mop`. Оба промаха тихие: папет уезжает на узел, где образа
    # его проекта нет, и висит pending, пока кто-нибудь не прочитает лог задачи
    # на узле.
    ("mop", "mop2", False),
    ("mop", "not-mop", False),
    ("op", "mop", False),
    ("mop", "mopmop", False),

    # `any` — слово целиком, а не приставка: узел, объявивший `anything`,
    # обслуживает проект `anything`, и только его.
    ("mop", "anything", False),
    ("anything", "anything", True),

    # Имя проекта — basename репозитория, в нём бывают точка и дефис. Точка в
    # незаэкранированном выражении совпадает с чем угодно.
    ("my.proj", "my.proj", True),
    ("my.proj", "myXproj", False),
    ("my-proj", "my-proj", True),
]


# Спека, зарегистрированная до раскола врапера и ограничения размещения,
# опасна на узле-гипервизоре: старый врапер разворачивает папета прямо на узле,
# а ограничения, которое не пустило бы его туда, в ней нет. Признак должен быть
# чистой функцией: иначе узнать об этом можно только из лога задачи на узле,
# куда мастер проекта не смотрит.
def job(outer=True, constrained=True):
    j = spec.job_spec("pu-mop-1", ORIGIN)["Job"]
    if not outer:
        j["TaskGroups"][0]["Tasks"][0]["Config"]["args"] = ["-c", "старый врапер"]
        j["TaskGroups"][0]["Tasks"][0]["Env"].pop("PU_WRAPPER", None)
    if not constrained:
        j["Constraints"] = None
    return j


STALE = [
    ("сегодняшняя спека", job(), False),
    ("без внешнего врапера", job(outer=False), True),
    ("без ограничения размещения", job(constrained=False), True),
    ("без того и другого", job(outer=False, constrained=False), True),
]


# (имя, origin, профиль, cont): оба профиля (без ключа и с ключом и картой
# моделей), профиль по умолчанию, проект с точкой в имени (экранирование в
# ограничении) и разовый подъём с историей. Драйвера среди входов нет: спека
# от него не зависит — внешний врапер один на все тела (docs/DRIVER.md).
SPEC_INPUTS = [
    ("pu-mop-1", "git@git.example.dev:someone/mop.git", "claude", False),
    ("pu-mop-2", "git@git.example.dev:someone/mop.git", None, False),
    ("pu-rugent-3", "https://git.example.dev/rugent/rugent.git", "glm", False),
    ("pu-my.proj-1", "git@git.example.dev:someone/my.proj.git", "glm", True),
]


def render(name, origin, profile, cont):
    """Спека ровно так, как её увидит Nomad: JSON, в порядке ключей."""
    return json.dumps(spec.job_spec(name, origin, profile, cont=cont),
                      ensure_ascii=False, indent=1)


def check_snapshot():
    """Спека для закреплённых входов совпадает со слепком байт в байт, а
    спека со слепка не читается устаревшей. STATUS: FIXED — see #152"""
    with open(SNAPSHOT) as f:
        want = json.load(f)
    bad = 0
    for inputs in SPEC_INPUTS:
        key = " ".join(str(x) for x in inputs)
        got = render(*inputs)
        if got != want.get(key):
            bad += 1
            print(f"FAILED  rendered spec for {key} differs from the snapshot")
        elif spec.spec_is_stale(json.loads(want[key])["Job"]):
            bad += 1
            print(f"FAILED  the snapshot spec for {key} reads as stale")
    if set(want) != {" ".join(str(x) for x in i) for i in SPEC_INPUTS}:
        bad += 1
        print("FAILED  the snapshot and SPEC_INPUTS disagree on the cases")
    return bad, len(SPEC_INPUTS) + 1


def main():
    if sys.argv[1:] == ["--snapshot"]:
        # Снять слепок заново: только осознанно, когда спека меняется нарочно
        # и все папеты всё равно перерегистрируются.
        with open(SNAPSHOT, "w") as f:
            json.dump({" ".join(str(x) for x in i): render(*i) for i in SPEC_INPUTS},
                      f, ensure_ascii=False, indent=1)
            f.write("\n")
        return 0
    bad, cases = check_snapshot()

    for what, j, want in STALE:
        cases += 1
        if spec.spec_is_stale(j) != want:
            bad += 1
            print(f"FAILED  {what}: спека "
                  f"{'признана устаревшей' if not want else 'признана свежей'}, "
                  f"ждали обратного")

    for project, meta, want in CASES:
        cases += 1
        got = serves(project, meta)
        if got != want:
            bad += 1
            print(f"FAILED  project {project!r} on a node announcing {meta!r}: "
                  f"got {got}, wanted {want}")

    # Ограничение обязано быть в каждой спеке: папет без него садится куда
    # угодно, и вся проверка становится украшением.
    cases += 1
    if project_constraint(spec.job_spec("pu-mop-1", ORIGIN)) is None:
        bad += 1
        print("FAILED  a job spec without the project constraint schedules anywhere")

    # Проект берётся из ORIGIN, а не из имени: имя — производное, и разойтись
    # они могут только при ручной регистрации, где ошибка и опаснее всего.
    cases += 1
    c = project_constraint(spec.job_spec("pu-anything-7", ORIGIN))
    if not re.search(c["RTarget"], "mop"):
        bad += 1
        print("FAILED  the constraint must follow the origin's project, not the name")

    # Врапер -- шелл в base64 спеки, и синтаксическая ошибка в нём видна
    # только на узле, падением каждого подъёма. bash -n ловит её здесь.
    cases += 1
    import subprocess
    r = subprocess.run(["bash", "-n"], input=spec.WRAPPER, text=True,
                       capture_output=True)
    if r.returncode:
        bad += 1
        print(f"FAILED  the wrapper is not valid bash: {r.stderr.strip()}")

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
