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


def job_meta(version):
    """Сегодняшняя спека с другой версией шаблона; None — без ключа вовсе."""
    j = job()
    j["Meta"] = dict(j["Meta"])
    if version is None:
        j["Meta"].pop("mop_spec", None)
    else:
        j["Meta"]["mop_spec"] = version
    return j


STALE = [
    ("сегодняшняя спека", job(), False),
    ("без внешнего врапера", job(outer=False), True),
    ("без ограничения размещения", job(constrained=False), True),
    ("без того и другого", job(outer=False, constrained=False), True),
    # #174: спека зарегистрирована прежним шаблоном — старый врапер, прежний
    # список переменных. Проходит обе проверки выше и работает, но не тем,
    # что собрал бы сегодняшний mop: так после #155 жил пятый папет чужого
    # проекта, и ничто его не выдавало.
    ("без версии шаблона (зарегистрирована до #174)", job_meta(None), True),
    ("с чужой версией шаблона", job_meta("0123456789ab"), True),
]


def check_template_version():
    """HYPOTHESIS (#174): spec_is_stale смотрел только на внешний врапер и
    ограничение размещения, и спека прежнего шаблона читалась свежей.
    SOLUTION: версия шаблона в Meta — хеш того, что общее у спек всех
    папетов, без значений конкретного папета. STATUS: FIXED — see #174"""
    bad, cases = 0, 0
    versions = {(spec.job_spec(n, o, p, cont=c)["Job"].get("Meta") or {}).get("mop_spec")
                for n, o, p, c in SPEC_INPUTS}
    cases += 1
    if len(versions) != 1 or None in versions:
        bad += 1
        print(f"FAILED  the template version must be one for every puppet: {versions}")
    # Одна строка врапера — другая версия: иначе правка врапера снова
    # прошла бы мимо doctor.
    cases += 1
    keep = spec.WRAPPER
    try:
        spec.WRAPPER = keep + "\n# one more line\n"
        other = (spec.job_spec(*SPEC_INPUTS[0][:3])["Job"].get("Meta") or {}).get("mop_spec")
    finally:
        spec.WRAPPER = keep
    if other in versions or other is None:
        bad += 1
        print("FAILED  a changed wrapper line must change the template version")
    # Версия не зависит от реестра профилей: удалённый MOP_DEFAULT_LLM иначе
    # ронял spec_is_stale, а ростер глотал падение как «спека свежая» —
    # ровно та тихая ошибка, которую закрывает #174.
    cases += 1
    keep = os.environ["MOP_DEFAULT_LLM"]
    try:
        os.environ["MOP_DEFAULT_LLM"] = "no-such-profile"
        got = spec.current_version()
    except Exception as e:
        got = f"raised {e!r}"
    finally:
        os.environ["MOP_DEFAULT_LLM"] = keep
    if got not in versions:
        bad += 1
        print(f"FAILED  current_version with a removed default profile: {got!r}, "
              f"wanted {versions}")
    cases += 1
    if spec.current_version() not in versions:
        bad += 1
        print("FAILED  current_version must equal the version job_spec puts in Meta")
    return bad, cases


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


# ── #155: пути узла — переменными спеки ───────────────────────────────────────
# HYPOTHESIS: врапер сам собирал пути узла ($HOME/puppets/$PU_NAME,
# target-$PU_NAME, secrets.env, bus-$PU_PROJECT.json, project-secrets/) — копии
# driver.clone_dir, driver.target_dir и соседей, которые разъезжаются молча.
# А CARRY в `mop driver run` перепечатывал список переменных спеки руками.
# SOLUTION: пути считает job_spec (driver.*) и кладёт в окружение задачи,
# врапер их только читает; CARRY — список ключей самой спеки (PU_CARRY).
#
# Где каждый путь окажется на узле, не меняется: OLD_PATHS — ровно строки
# врапера до #155, и значение каждой новой переменной обязано совпасть с тем,
# что из них получилось бы.
OLD_PATHS = {
    "PU_CLONE": "$HOME/puppets/$PU_NAME",
    "PU_TARGET": "$HOME/.cache/target-$PU_NAME",
    "PU_SECRETS": "$HOME/.config/mop/secrets.env",
    "PU_PROJECT_CREDS": "$HOME/.config/mop/bus-$PU_PROJECT.json",
    "PU_SECRETS_DIR": "$HOME/.config/mop/project-secrets/$PU_PROJECT",
}
# Список ключей, который переливал старый `mop driver run`: спека,
# зарегистрированная до #155, PU_CARRY не несёт, и её папет обязан подняться
# после раскатки до перерегистрации.
LEGACY_CARRY = ["PU_NAME", "PU_ORIGIN", "PU_PROJECT", "PU_SEED", "PU_CONTINUE",
                "PU_LLM", "PU_LLM_ENV", "PU_LLM_KEY_VAR", "PU_LLM_AUTH_VAR",
                "HOME", "PATH"]


def expand(template, env):
    return re.sub(r"\$(HOME|PU_NAME|PU_PROJECT)\b", lambda m: env[m.group(1)], template)


def check_wrapper_paths():
    """STATUS: FIXED — see #155"""
    from mop import driver
    from mop.cli.driver import run
    bad, cases = 0, 0
    for name, origin, profile, cont in SPEC_INPUTS:
        env = spec.job_spec(name, origin, profile, cont=cont)["Job"]["TaskGroups"][0]["Tasks"][0]["Env"]
        project = env.get("PU_PROJECT")
        # (b) каждый путь — там же, где его строил старый врапер, и там же,
        # где его строит остальной код.
        want = {k: expand(t, env) for k, t in OLD_PATHS.items()}
        same_as = {"PU_CLONE": driver.clone_dir(name),
                   "PU_TARGET": driver.target_dir(name),
                   "PU_SECRETS": getattr(driver, "SECRETS_FILE", None),
                   "PU_PROJECT_CREDS": getattr(driver, "project_creds", lambda p: None)(project),
                   "PU_SECRETS_DIR": getattr(driver, "project_secrets_dir", lambda p: None)(project)}
        for k in OLD_PATHS:
            cases += 1
            if env.get(k) != want[k] or same_as[k] != want[k]:
                bad += 1
                print(f"FAILED  {k} for {name}: spec {env.get(k)!r}, driver {same_as[k]!r}, "
                      f"the old wrapper {want[k]!r}")
        # (c) в тело едут ровно ключи спеки, кроме самого врапера.
        cases += 1
        body = [k for k in env if k not in ("PU_WRAPPER", "PU_CARRY")]
        got = getattr(run, "carry", lambda e: None)(env)
        if (env.get("PU_CARRY") or "").split(",") != body or got != body:
            bad += 1
            print(f"FAILED  carry for {name}: PU_CARRY {env.get('PU_CARRY')!r}, "
                  f"run.carry {got!r}, spec keys {body!r}")
        # (a) всё, что врапер читает, в спеке есть и в тело доезжает.
        cases += 1
        reads = set(re.findall(r"\$\{?(PU_[A-Z_]+)", spec.WRAPPER))
        missing = sorted(reads - set(body))
        if missing:
            bad += 1
            print(f"FAILED  the wrapper reads {missing}, which the spec does not carry into the body")
    # Спека старше #155 едет старым списком: иначе папет падает на первом же
    # рестарте между раскаткой и перерегистрацией.
    cases += 1
    old_env = {k: "x" for k in LEGACY_CARRY}
    if getattr(run, "carry", lambda e: None)(old_env) != LEGACY_CARRY:
        bad += 1
        print("FAILED  a spec without PU_CARRY must carry the pre-#155 list")
    # Врапер путей узла больше не собирает.
    for pattern in ("$HOME/puppets", "target-$PU_NAME", "secrets.env",
                    "project-secrets", "bus-$PU_PROJECT"):
        cases += 1
        if pattern in spec.WRAPPER:
            bad += 1
            print(f"FAILED  the wrapper still builds a node path itself: {pattern}")
    # Пустой путь — отказ до первой команды: `rm -rf "$d"` и free_dir над
    # пустой строкой задели бы всё, что есть в теле.
    cases += 1
    for k in OLD_PATHS:
        if f'${{{k}:?' not in spec.WRAPPER:
            bad += 1
            print(f"FAILED  the wrapper must refuse an empty {k} before touching anything")
            break
    # Дубль PU_PROJECT в литерале словаря — молчаливый: второй перетирает
    # первый, и правка одного из них ничего не значит.
    cases += 1
    import ast
    tree = ast.parse(open(spec.__file__).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if len(keys) != len(set(keys)):
                bad += 1
                print(f"FAILED  duplicate keys in a dict literal of spec.py: {keys}")
                break
    return bad, cases


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
    vbad, vcases = check_template_version()
    bad += vbad
    cases += vcases
    pbad, pcases = check_wrapper_paths()
    bad += pbad
    cases += pcases

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
