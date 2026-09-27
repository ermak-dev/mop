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

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, FakeNomad, patched, patched_env  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

# ── #152: слепок спеки ────────────────────────────────────────────────────────
# Спека зарегистрированного папета живёт в Nomad, и любое её расхождение с
# тем, что соберёт следующая версия кода, видно только по симптому:
# spec_is_stale метит весь пул, doctor перерегистрирует каждого папета на обеих
# установках. Поэтому перенос job_spec между модулями сверяется со слепком
# байт в байт, а не «по смыслу».
#
# Всё, что спека читает из установки, прибито окружением ДО импорта mop
# (окружение старше .env и node.env, см. mop/common/config.py): иначе слепок совпадал
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

from mop.common import config  # noqa: E402
from mop.server import spec  # noqa: E402

# Просьбы проектов (#197) спека читает файлом сервера: слепок снимается без
# него -- так спеку видит установка, где deploy просьб ещё не привозил.
spec.ASKS_FILE = os.path.join(os.path.dirname(SNAPSHOT), "no-such-project-asks.json")


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


def check_template_version(c):
    """HYPOTHESIS (#174): spec_is_stale смотрел только на внешний врапер и
    ограничение размещения, и спека прежнего шаблона читалась свежей.
    SOLUTION: версия шаблона в Meta — хеш того, что общее у спек всех
    папетов, без значений конкретного папета. STATUS: FIXED — see #174"""
    versions = {(spec.job_spec(n, o, p, cont=cont)["Job"].get("Meta") or {}).get("mop_spec")
                for n, o, p, cont in SPEC_INPUTS}
    c.check("the template version must be one for every puppet",
            not (len(versions) != 1 or None in versions), versions)
    # Одна строка врапера — другая версия: иначе правка врапера снова
    # прошла бы мимо doctor.
    with patched(spec, WRAPPER=spec.WRAPPER + "\n# one more line\n"):
        other = (spec.job_spec(*SPEC_INPUTS[0][:3])["Job"].get("Meta") or {}).get("mop_spec")
    c.check("a changed wrapper line must change the template version",
            not (other in versions or other is None))
    # Версия не зависит от реестра профилей: удалённый MOP_DEFAULT_LLM иначе
    # ронял spec_is_stale, а ростер глотал падение как «спека свежая» —
    # ровно та тихая ошибка, которую закрывает #174.
    with patched_env(MOP_DEFAULT_LLM="no-such-profile"):
        try:
            got = spec.current_version()
        except Exception as e:
            got = f"raised {e!r}"
    c.check("current_version with a removed default profile", not (got not in versions),
            f"{got!r}, wanted {versions}")
    c.check("current_version must equal the version job_spec puts in Meta",
            not (spec.current_version() not in versions))


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


def check_snapshot(c):
    """Спека для закреплённых входов совпадает со слепком байт в байт, а
    спека со слепка не читается устаревшей. STATUS: FIXED — see #152"""
    with open(SNAPSHOT) as f:
        want = json.load(f)
    for inputs in SPEC_INPUTS:
        key = " ".join(str(x) for x in inputs)
        got = render(*inputs)
        if c.check(f"rendered spec for {key} differs from the snapshot",
                   not (got != want.get(key))):
            c.check(f"the snapshot spec for {key} reads as stale",
                    not (spec.spec_is_stale(json.loads(want[key])["Job"])))
    c.check("the snapshot and SPEC_INPUTS disagree on the cases",
            not (set(want) != {" ".join(str(x) for x in i) for i in SPEC_INPUTS}))


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


def check_wrapper_paths(c):
    """STATUS: FIXED — see #155"""
    from mop import driver
    from mop.cli.driver import run
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
            c.check(f"{k} for {name}", not (env.get(k) != want[k] or same_as[k] != want[k]),
                    f"spec {env.get(k)!r}, driver {same_as[k]!r}, the old wrapper {want[k]!r}")
        # (c) в тело едут ровно ключи спеки, кроме самого врапера.
        body = [k for k in env if k not in ("PU_WRAPPER", "PU_CARRY")]
        got = getattr(run, "carry", lambda e: None)(env)
        c.check(f"carry for {name}",
                not ((env.get("PU_CARRY") or "").split(",") != body or got != body),
                f"PU_CARRY {env.get('PU_CARRY')!r}, run.carry {got!r}, spec keys {body!r}")
        # (a) всё, что врапер читает, в спеке есть и в тело доезжает.
        reads = set(re.findall(r"\$\{?(PU_[A-Z_]+)", spec.WRAPPER))
        missing = sorted(reads - set(body))
        c.check("the wrapper reads only what the spec carries into the body", not (missing),
                f"the wrapper reads {missing}, which the spec does not carry into the body")
    # Спека старше #155 едет старым списком: иначе папет падает на первом же
    # рестарте между раскаткой и перерегистрацией.
    old_env = {k: "x" for k in LEGACY_CARRY}
    c.check("a spec without PU_CARRY must carry the pre-#155 list",
            not (getattr(run, "carry", lambda e: None)(old_env) != LEGACY_CARRY))
    # Врапер путей узла больше не собирает.
    for pattern in ("$HOME/puppets", "target-$PU_NAME", "secrets.env",
                    "project-secrets", "bus-$PU_PROJECT"):
        c.check(f"the wrapper still builds a node path itself: {pattern}",
                not (pattern in spec.WRAPPER))
    # Пустой путь — отказ до первой команды: `rm -rf "$d"` и free_dir над
    # пустой строкой задели бы всё, что есть в теле.
    for k in OLD_PATHS:
        if not c.check(f"the wrapper must refuse an empty {k} before touching anything",
                       not (f'${{{k}:?' not in spec.WRAPPER)):
            break
    # Дубль PU_PROJECT в литерале словаря — молчаливый: второй перетирает
    # первый, и правка одного из них ничего не значит.
    import ast
    tree = ast.parse(open(spec.__file__).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if not c.check("duplicate keys in a dict literal of spec.py",
                           not (len(keys) != len(set(keys))), keys):
                break


def check_git_identity(c):
    """HYPOTHESIS (#167): в клоне папета нет git identity. Промежуточная форма
    (7f9c2a8) -- identity установки: MOP_GIT_NAME и MOP_GIT_EMAIL, четыре GIT_*
    в окружении задачи. Оператор решил иначе: автор коммита -- человек,
    прошедший проверку на шине, а не установка. GIT_* в окружении старше
    любого git config, и identity владельца в клоне они бы перебили.
    SOLUTION: настроек нет вовсе, спека не несёт ни одного GIT_* ни при каком
    окружении; identity ставит агент узла в клон по проверенному владельцу
    задания. Версия шаблона (#174) -- та же, что без настроек: на обеих
    установках они были пусты, набор ключей спеки не менялся, и перерегистрации
    не будет. STATUS: FIXED — see #167"""
    inputs = SPEC_INPUTS[0]
    unset = spec.current_version()
    gone = [k for k in ("MOP_GIT_NAME", "MOP_GIT_EMAIL")
            if k in config.SETTINGS or k in config.SERVER_SCOPED["mop-cluster"]]
    c.check("the installation-wide git identity must be gone", not (gone), gone)
    # Прежний код снимал обе переменные после блока безусловно (del), а не
    # возвращал прежние: до блока их нет -- patched_env даёт то же.
    with patched_env(MOP_GIT_NAME="Pool Bot", MOP_GIT_EMAIL="bot@example.dev"):
        env = spec.job_spec(*inputs[:3])["Job"]["TaskGroups"][0]["Tasks"][0]["Env"]
        version = spec.current_version()
    got = [k for k in env if k.startswith("GIT_")]
    c.check("the spec must carry no GIT_*, even with MOP_GIT_* in the environment",
            not (got), got)
    c.check("MOP_GIT_* must not move the template version", not (version != unset))


# ── #197: память папета -- его свойство ──────────────────────────────────────
# HYPOTHESIS: у папета три несвязанных числа памяти. Резерв Nomad --
# MOP_PUPPET_MEM_MB, потолок Nomad -- MOP_MEM_MB, оба только установки; на pve
# настоящая память тела -- `pct --memory` из образа, то есть из `.mop` проекта
# на момент сборки. Слоты врут, просьба проекта на host не действует вовсе.
# SOLUTION: одна функция: потолок -- просьба проекта (MOP_MEM_MB его `.mop`),
# иначе установки (.env поверх дефолта); резерв -- бюджет установки, но не
# выше потолка. Применяет Nomad на обоих драйверах.
# (просьбы проекта, потолок установки, бюджет установки, (резерв, потолок))
MEMORY = [
    ("no ask: the installation's ceiling", {}, 12288, 8192, (8192, 12288)),
    ("the ask over the installation", {"MOP_MEM_MB": "16384"}, 12288, 8192, (8192, 16384)),
    # Просьба ниже бюджета: резерв не выше потолка, иначе Nomad отвергнет
    # спеку (MemoryMaxMB < MemoryMB), а слоты пообещали бы больше, чем тело.
    ("an ask below the budget", {"MOP_MEM_MB": "6144"}, 12288, 8192, (6144, 6144)),
    ("an int ask from YAML", {"MOP_MEM_MB": 4096}, 12288, 8192, (4096, 4096)),
    ("other asks are not memory", {"MOP_DISK_GB": "50"}, 12288, 8192, (8192, 12288)),
    ("an installation ceiling below its budget", {}, 4096, 8192, (4096, 4096)),
]
# Просьба, которую нельзя применить: громко, а не молчаливая подмена
# значением установки -- проект просил другого.
BAD_ASKS = ["lots", "", "0", "-512", "8G", "1.5"]


def check_memory_197(c):
    """Цепочка памяти папета: дефолт -> .env -> `.mop` проекта.
    STATUS: FIXED — see #197"""
    for what, asks, ceiling, budget, want in MEMORY:
        c.expect(f"memory, {what}", spec.memory(asks, ceiling, budget), want)
    for ask in BAD_ASKS:
        try:
            got = spec.memory({"MOP_MEM_MB": ask}, 12288, 8192)
        except ValueError as e:
            c.check(f"memory, ask {ask!r}: the refusal does not name MOP_MEM_MB",
                    not ("MOP_MEM_MB" not in str(e)), e)
            continue
        c.fail(f"memory, ask {ask!r}", f"took it as {got}, wanted a refusal")


def check_project_asks_197(c):
    """Просьбы проектов на сервере: файл, который кладёт роль cluster из
    манифестов `mop server deploy`. Нет файла или нет проекта в нём -- значения
    установки, без падения: это любой проект до первого прогона.
    Просьбы проекта -- Project.of над таблицей read_asks (#204).
    STATUS: FIXED — see #197"""
    import tempfile
    from mop.common.domain import Project

    def project_asks(project, path):
        return Project.of(f"git@h:g/{project}.git", asks=spec.read_asks(path)).asks
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "project-asks.json")
        c.expect("project_asks without the file", project_asks("mop", path), {})
        with open(path, "w") as f:
            json.dump({"mop": {"MOP_MEM_MB": "6144", "MOP_CORES": "4"}}, f)
        for project, want in (("mop", {"MOP_MEM_MB": "6144", "MOP_CORES": "4"}),
                              ("rugent", {})):
            c.expect(f"project_asks({project!r})", project_asks(project, path), want)


def memory_constraint(rendered):
    """Ограничение про потолок памяти узла; None, если его нет. Литерал
    ключа нарочно: проверка читает то, что уедет в Nomad."""
    for c in rendered["Job"].get("Constraints") or []:
        if c.get("LTarget") == "${meta.mop_mem_cap_mb}":
            return c
    return None


def check_spec_memory_197(c):
    """Спека применяет память папета (#197): резерв и потолок Nomad -- из
    spec.memory для проекта папета, потолок едет в окружение задачи
    (PU_MEM_MB, его читает `mop driver run` pve), и узел берёт папета, только
    если его потолок (meta.mop_mem_cap_mb) не ниже потолка папета.
    Бюджет и потолок установки -- PINNED: 4096 и 8192.
    STATUS: FIXED — see #197"""
    import tempfile
    what = "spec memory, "
    with tempfile.TemporaryDirectory() as d, \
            patched(spec, ASKS_FILE=os.path.join(d, "project-asks.json")):
        with open(spec.ASKS_FILE, "w") as f:
            json.dump({"big": {"MOP_MEM_MB": "16384"}, "small": {"MOP_MEM_MB": "2048"},
                       "odd": {"MOP_MEM_MB": "8G"}, "plain": {"MOP_CORES": "8"}}, f)
        for project, want in (("big", (4096, 16384)), ("small", (2048, 2048)),
                              ("plain", (4096, 8192)), ("absent", (4096, 8192))):
            j = spec.job_spec(f"pu-{project}-1", f"git@git.example.dev:someone/{project}.git")
            task = j["Job"]["TaskGroups"][0]["Tasks"][0]
            res = task["Resources"]
            c.check(f"{what}{project}: MemoryMB/MemoryMaxMB",
                    (res.get("MemoryMB"), res.get("MemoryMaxMB")) == want, res)
            c.check(f"{what}{project}: PU_MEM_MB is the ceiling",
                    task["Env"].get("PU_MEM_MB") == str(want[1]), task["Env"].get("PU_MEM_MB"))
            c.check(f"{what}{project}: PU_MEM_MB is carried into the body",
                    "PU_MEM_MB" in task["Env"].get("PU_CARRY", "").split(","))
            cap = memory_constraint(j)
            c.check(f"{what}{project}: the node's cap constraint",
                    cap == {"LTarget": "${meta.mop_mem_cap_mb}", "Operand": ">=",
                            "RTarget": str(want[1])}, cap)
            c.check(f"{what}{project}: the project constraint stays",
                    project_constraint(j) is not None)
            c.check(f"{what}{project}: the ceiling reads back from the spec",
                    spec.ceiling_of(j["Job"]) == want[1], spec.ceiling_of(j["Job"]))
            c.check(f"{what}{project}: a fresh spec is not stale",
                    not spec.spec_is_stale(j["Job"]))
        try:
            spec.job_spec("pu-odd-1", "git@git.example.dev:someone/odd.git")
            c.check(f"{what}odd: an ask that cannot be applied is refused", False)
        except RuntimeError as e:
            c.check(f"{what}odd: the refusal names the puppet and the ask",
                    "pu-odd-1" in str(e) and "MOP_MEM_MB" in str(e), e)
    # Спека до #197: ни ограничения, ни потолка в нём.
    c.check(f"{what}a spec without the cap constraint has no ceiling",
            spec.ceiling_of({"Constraints": [{"LTarget": "${meta.mop_projects}"}]}) is None)


# Как Nomad 1.10 сравнивает `>=` (scheduler/feasible.go, checkOrder): обе
# стороны целые (strconv.ParseInt, основание 10) -- как целые, иначе обе
# float -- как float, иначе лексически. (слева -- meta узла, справа -- потолок
# папета, сядет ли)
ORDER = [
    ("32768", "16384", True),
    ("16384", "16384", True),
    ("8192", "16384", False),
    # Лексически "9" > "10" и "9000" > "12288": численное сравнение здесь
    # единственное, что отличает потолок от строки.
    ("9", "10", False),
    ("9000", "12288", False),
    ("10000", "9999", True),
    ("+16384", "16384", True),
    ("16384.5", "16384", True),
    # Не число -- лексический порядок Nomad, и именно поэтому deploy
    # отвергает потолок узла не из одного целого числа.
    ("32G", "16384", True),
    (" 32768", "16384", False),
    ("", "16384", False),
]


def check_nomad_order_197(c):
    """Зеркало checkOrder Nomad для диагноза unserved: он обязан отвечать
    ровно как планировщик. STATUS: FIXED — see #197"""
    for left, right, want in ORDER:
        c.expect(f"nomad_order({left!r} >= {right!r})", spec.nomad_order(">=", left, right), want)


# Спека собирается в подпроцессе с данным MOP_DRIVER: настройки читаются
# при импорте, и сменить драйвер можно только свежим процессом. Сеть в нём
# закрыта на уровне сокета до первого импорта.
_SPEC_UNDER_DRIVER = r"""
import json, socket, sys
def refuse(*a, **k):
    raise RuntimeError("the check tried to reach the network")
socket.socket.connect = socket.socket.connect_ex = refuse
sys.path.insert(0, sys.argv[1])
from mop.server import spec
spec.ASKS_FILE = "/nonexistent/project-asks.json"
print(json.dumps(spec.job_spec("pu-mop-1", "git@h:g/mop.git"), sort_keys=True))
"""


def check_driver_free_183(c):
    """HYPOTHESIS (#183): спеку строит сервис кластера на сервере, а пути в её
    окружении (#155) берутся из mop/driver -- не зависят ли они от драйвера
    машины, которая строит спеку, а не узла, куда встанет папет?
    RESULT: не зависят: пути читают только MOP_HOME, MOP_DRIVER на пути спеки
    не читается вовсе (разведка #183). Проверка закрепляет это: правка,
    которая сделает значение спеки драйверным, упадёт здесь. Узловые
    MOP_HOME/MOP_USER/MOP_PUPPET_SEED/MOP_MEM_MB в спеке -- отдельная задача
    слоя deploy (#186).
    STATUS: FIXED — see #183"""
    import subprocess
    got = {}
    for drv in ("host", "pve", "no-such-driver"):
        env = dict(os.environ, MOP_DRIVER=drv, MOP_SERVER_LAN="192.0.2.1",
                   MOP_HOME="/home/pool", MOP_USER="pool")
        env.pop("MOP_BUS_CONFIG", None)
        root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        r = subprocess.run([sys.executable, "-c", _SPEC_UNDER_DRIVER, root], env=env,
                           capture_output=True, text=True)
        if not c.check(f"job_spec under MOP_DRIVER={drv}", not (r.returncode), r.stderr[-300:]):
            continue
        got[drv] = json.loads(r.stdout)
    for drv in ("pve", "no-such-driver"):
        if drv in got and "host" in got:
            spec_host = got["host"]
            # PU_CONTINUE -- время; при cont=False оно пустое и равно.
            c.check(f"job_spec differs under MOP_DRIVER={drv} from host",
                    not (got[drv] != spec_host),
                    "the spec is built on the server, not on the puppet's node")


# ── #223: хуки Claude Code — через --settings врапера ─────────────────────────
# HYPOTHESIS: состояние папета угадывается по экрану tmux и промахивается
# (24.09 pu-mop-2/3 умерли на «Login expired» посреди хода, ростер 1.5 часа
# читал их `idle (uncommitted)`). session.py hook (#222) пишет исход хода, но
# claude папета его не зовёт.
# SOLUTION: врапер на каждом старте пишет файл хуков вне клона и отдаёт его
# claude через --settings. Не ~/.claude/settings.json: на host-узле его делят
# все claude машины, включая оператора и мастера.
HOOK_EVENTS = {"SessionStart", "UserPromptSubmit", "Stop", "StopFailure",
               "PostModelSwitch"}
HOOKS_BEGIN = "# --- claude hooks (#223) ---"
HOOKS_END = "# --- end of claude hooks ---"


def hooks_section():
    """Кусок врапера, который пишет файл хуков, -- ровно тот текст, что едет."""
    w = spec.WRAPPER
    if HOOKS_BEGIN not in w or HOOKS_END not in w:
        return None
    return w[w.index(HOOKS_BEGIN):w.index(HOOKS_END)]


def run_hooks_section(section, home, path):
    """Кусок хуков -- как в теле: set -e, свой HOME, имя папета. -> процесс,
    чей stdout -- корень пакета, путь файла и аргумент claude, по строке."""
    import subprocess
    script = ("set -e\n" + section
              + '\nprintf "%s\\n%s\\n%s" "$mop_root" "$hooks" "$settings"\n')
    return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                          env={"HOME": home, "PU_NAME": "pu-mop-1", "PATH": path})


def _hooks_written(c, section):
    """(a)-(c) проверки #223: файл хуков, который пишет врапер. Нужен jq."""
    import shutil
    import subprocess
    import tempfile
    home = tempfile.mkdtemp()
    try:
        # Кусок исполняется как в теле: set -e, свой HOME, имя папета. Второй
        # прогон -- поверх первого: так идёт каждый рестарт.
        for _ in range(2):
            r = run_hooks_section(section, home, os.environ["PATH"])
        if not c.check("the hooks section fails", not (r.returncode), r.stderr.strip()):
            return
        mop_root, hooks, settings = r.stdout.split("\n")
        c.check("a written hooks file must give claude --settings",
                not (settings != f"--settings {hooks}"), f"got {settings!r}")
        # (a) файл вне клона и вне общего ~/.claude/settings.json
        c.check("the hooks file must live in mop's own config",
                not (not hooks.startswith(home + "/.config/mop/") or "/.claude/" in hooks), hooks)
        left = [f for f in os.listdir(os.path.dirname(hooks)) if f.endswith(".tmp")]
        c.check("the hooks file is written through a temp file", not (left), f"left: {left}")
        # (b) валидный JSON, ровно пять событий, каждое -- охраняемый вызов
        try:
            with open(hooks) as f:
                doc = json.load(f)
            events = doc["hooks"]
        except (OSError, ValueError, KeyError, TypeError) as e:
            c.fail("the hooks file is not the settings JSON", e)
            return
        c.check("hook events", not (set(events) != HOOK_EVENTS),
                f"{sorted(events)}, wanted {sorted(HOOK_EVENTS)}")
        # session.py -- от того же корня, что и лаунчер mop во внешнем врапере:
        # другая сторона соглашения -- driver.SESSION_PY обоих драйверов.
        outer = re.search(r'exec "(.+)/bin/mop"', spec.OUTER)
        session_py = mop_root.replace("$HOME", home) + "/mop/session.py"
        c.check("the hooks' package root and the launcher's must be one root",
                not (not outer or outer.group(1) != "$HOME/mop" or mop_root != home + "/mop"),
                f"{mop_root!r} and {outer and outer.group(1)!r}")
        cmds = set()
        for name, groups in events.items():
            for g in groups:
                for h in g.get("hooks", []):
                    cmds.add((h.get("type"), h.get("command"), h.get("timeout")))
        want = (f"python3 {session_py} hook >/dev/null 2>&1 || true")
        c.check(f"every event must run exactly {want!r}, timeout 5",
                not (cmds != {("command", want, 5)} or
                     any(len(g) != 1 or len(g[0].get("hooks", [])) != 1 for g in events.values())),
                f"got {cmds}")
        # (c) охрана держит против любого session.py на узле: старый без
        # глагола hook выходит 2 (Stop продолжил бы ход вечно), болтливый
        # попал бы в контекст модели, отсутствующий -- тоже ненулевой выход.
        os.makedirs(os.path.dirname(session_py), exist_ok=True)
        for what, body in (("an old session.py", "import sys\nprint('usage')\nsys.exit(2)\n"),
                           ("a chatty session.py", "print('context!')\n"),
                           ("no session.py", None)):
            if body is None:
                os.remove(session_py)
            else:
                with open(session_py, "w") as f:
                    f.write(body)
            for _, cmd, _ in cmds:
                r = subprocess.run(["sh", "-c", cmd], input="{}", text=True,
                                   capture_output=True)
                c.check(f"{what}: the hook must be silent and exit 0",
                        not (r.returncode or r.stdout or r.stderr),
                        f"got {r.returncode} {r.stdout!r} {r.stderr!r}")
    finally:
        shutil.rmtree(home)


def check_claude_hooks_223(c):
    """STATUS: FIXED — see #223"""
    import base64
    import shutil
    import subprocess  # noqa: F401
    import tempfile
    section = hooks_section()
    if not c.check("the wrapper has no claude hooks section", not (section is None)):
        return
    # (a)-(c) пишет файл хуков врапер, и пишет его jq (он в теле: edit_json).
    # Без jq на машине проверок врапер честно уходит в ветку «без хуков»,
    # и эти проверки читались бы поломкой (#230: python:3.x-slim). Пропуск --
    # громкий и в конце: (d)-(f), в том числе ветка без jq, идут всегда.
    no_jq = shutil.which("jq", path=os.environ["PATH"]) is None
    if not no_jq:
        _hooks_written(c, section)
    # (d) хуки -- наблюдение, а не условие подъёма: файл не записался --
    # папет встаёт без --settings и говорит об этом, а не падает. Упавший
    # врапер -- это папет, который не поднимается вовсе, из-за того, что
    # должно было лишь сообщать о нём.
    for what, broken in (("no jq in the body", "nojq"), ("~/.config/mop is a file", "file")):
        home = tempfile.mkdtemp()
        try:
            path = os.environ["PATH"]
            if broken == "nojq":
                path = os.path.join(home, "bin")
                os.makedirs(path)
                for tool in ("mkdir", "dirname", "mv", "rm", "cat", "printf"):
                    real = shutil.which(tool)
                    if real:
                        os.symlink(real, os.path.join(path, tool))
            else:
                os.makedirs(os.path.join(home, ".config"))
                open(os.path.join(home, ".config", "mop"), "w").close()
            r = run_hooks_section(section, home, path)
            settings = r.stdout.split("\n")[-1] if r.stdout else None
            leftovers = [os.path.join(dp, f) for dp, _, fs in os.walk(home)
                         for f in fs if f.endswith(".tmp")]
            c.check(f"{what}: the puppet must start without --settings and say so",
                    not (r.returncode or settings != "" or not r.stderr.strip() or leftovers),
                    f"got exit {r.returncode}, settings {settings!r}, "
                    f"stderr {r.stderr.strip()!r}, temp files {leftovers}")
        finally:
            shutil.rmtree(home)
    # (e) claude стартует с этим файлом, когда он есть
    launch = re.search(r'^claude_args="([^"]*)"', spec.WRAPPER, re.M)
    c.check("the claude launch line must carry $settings",
            not (not launch or "$settings" not in launch.group(1)
                 or "$claude_args" not in spec.WRAPPER.split("tmux -L \"$PU_NAME\" new-session")[-1]))
    # (f) врапер едет base64 (#155): доллары одинарные, а в открытой части
    # спеки нет ни JSON хуков, ни `${…}`, кроме меты узла.
    c.check("the wrapper travels base64: a $$ in it would reach the body literally",
            not ("$$" in spec.WRAPPER))
    for inputs in SPEC_INPUTS:
        job = spec.job_spec(*inputs[:3], cont=inputs[3])
        env = job["Job"]["TaskGroups"][0]["Tasks"][0]["Env"]
        c.check(f"{inputs[0]}: the wrapper in the spec has no hooks",
                not ('settings="--settings $hooks"' not in
                     base64.b64decode(env["PU_WRAPPER"]).decode()))
        env["PU_WRAPPER"] = ""
        plain = json.dumps(job)
        subs = set(re.findall(r"\$\{[^}]*\}", plain))
        c.check(f"{inputs[0]}: hooks or substitutions outside the base64 wrapper",
                not ("hook" in plain or not subs <= {spec.PROJECTS_TARGET, spec.MEM_CAP_TARGET}),
                sorted(subs))
    if no_jq:
        hermetic.skip("the claude hooks wrapper block", "no jq on this machine")


# ── #265: мета джоба -- одно значение, JobMeta ───────────────────────────
# HYPOTHESIS: Meta джоба (origin, llm, branch, mop_spec) читают сырым .get в
# семи модулях с разными умолчаниями, профиль -- тремя путями, а
# перерегистрацию по мете пишут четыре места, и они разошлись: image.clear
# и image.restore теряли Meta.branch, и после сборки образа папет мастера в
# своей ветке (#256) поднимался на origin/HEAD.
# SOLUTION: JobMeta (mop/common/domain.py) -- from_job/from_meta/to_meta в
# порядке ключей job_spec; читатели -- через него; профиль -- одна функция
# llm.of_meta; перерегистрация на сервере -- spec.respec, и clear записывает
# ветку, а restore и лечение её несут.
# STATUS: FIXED — see #265
BRANCHED = ("pu-mop-3", "git@git.example.dev:someone/mop.git", "claude", False, "feat/256-x")


def check_job_meta_265(c):
    from mop.common import domain
    JobMeta = getattr(domain, "JobMeta", None)
    c.check("mop.common.domain has no JobMeta", not (JobMeta is None))
    # Круг без потерь, байт в байт и в том же порядке ключей.
    for name, origin, profile, cont, *branch in (SPEC_INPUTS + [BRANCHED]) if JobMeta else []:
        meta = spec.job_spec(name, origin, profile, cont=cont,
                             branch=branch[0] if branch else None)["Job"]["Meta"]
        m = JobMeta.from_meta(meta)
        back = m.to_meta()
        c.check(f"JobMeta round trip for {name}", not (json.dumps(back) != json.dumps(meta)),
                f"{back} != {meta}")
        c.check(f"from_job and from_meta disagree for {name}",
                not (JobMeta.from_job({"Meta": meta}) != m))
        c.check(f"JobMeta.project for {name}",
                not (m.project != spec.driver.project_of(origin)), repr(m.project))
    # Аренда кредита (#284): Meta.cred едет как ветка -- только когда есть.
    if JobMeta is not None:
        with_cred = spec.job_spec(*BRANCHED[:3], branch=BRANCHED[4], cred="anton")["Job"]["Meta"]
        c.expect("job_spec(cred=...) puts cred in Meta", with_cred.get("cred"), "anton")
        c.check("JobMeta round trip keeps cred",
                JobMeta.from_meta(with_cred).cred == "anton"
                and json.dumps(JobMeta.from_meta(with_cred).to_meta()) == json.dumps(with_cred))
        without = spec.job_spec(*BRANCHED[:3], branch=BRANCHED[4])["Job"]["Meta"]
        c.check("no cred -> no cred key in Meta", "cred" not in without, without)
        c.expect("respec carries cred", spec.respec("pu-mop-3", JobMeta.from_meta(with_cred))
                 ["Job"]["Meta"].get("cred"), "anton")
    empty = JobMeta.from_job({}) if JobMeta else None
    c.check("a job without Meta",
            not (empty is not None and (empty.origin, empty.llm, empty.branch, empty.spec_version,
                                        empty.project) != (None, None, None, None, "")), empty)
    if JobMeta is not None:
        try:
            JobMeta.from_meta({"origin": "o", "llm": "l"}).__setattr__("llm", "x")
            c.fail("JobMeta must be frozen")
        except AttributeError:
            pass

    # Сборка образа снимает папетов и поднимает их с веткой мастера.
    from mop.server import image
    from mop.common import puppets
    job = spec.job_spec(*BRANCHED[:3], branch=BRANCHED[4])["Job"]
    called = []
    real = spec.job_spec
    # Nomad -- поддельный, параметром (#275), а не подменой атрибутов модуля.
    fake = FakeNomad()
    with patched(image, project_rows=lambda project, api=None: [
                {"name": job["ID"], "node": "n1", "job": job, "container": True,
                 "state": "free", "kind": None}]), \
            patched(puppets, _wait_stopped=lambda name: None,
                    wipe=lambda node, name, *a, **k: None), \
            patched(spec, job_spec=lambda *a, **k: called.append((a, k)) or real(*a, **k)):
        gone = image.clear("mop", force=True, api=fake)
        image.restore(gone, api=fake)
    got = [k.get("branch", a[4] if len(a) > 4 else None) for a, k in called]
    c.check("image.restore must raise the puppet on its branch",
            not (got != [BRANCHED[4]]), f"job_spec calls {called}")

    # Лечение doctor'а (update) несёт ветку папета.
    sent = []
    with patched(puppets, _cluster=lambda verb, **kw: sent.append((verb, kw)) or (
            {"meta": job["Meta"]} if verb == "spec" else {})):
        puppets.treat({"action": "update", "alloc": None, "name": job["ID"]})
    upd = [kw for verb, kw in sent if verb == "update"]
    c.check("treat must re-register with the puppet's branch",
            not (not upd or upd[0].get("branch") != BRANCHED[4]
                 or upd[0].get("origin") != BRANCHED[1]), sent)

    # Мету читают только через JobMeta: сырых чтений её ключей нет нигде, кроме
    # domain.py (node Meta в nomad.py -- мета узлов, не джоба).
    root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    raw = re.compile(r'get\("Meta"\)|\b(meta|m)\.get\("(origin|llm|branch|mop_spec)"'
                     r'|\.get\(SPEC_META\)')
    found = []
    for dp, _, fs in os.walk(os.path.join(root, "mop")):
        for f in fs:
            path = os.path.join(dp, f)
            rel = os.path.relpath(path, root)
            if not f.endswith(".py") or rel in ("mop/common/domain.py", "mop/server/nomad.py"):
                continue
            for n, line in enumerate(open(path), 1):
                if raw.search(line.split("#")[0]):
                    found.append(f"{rel}:{n}: {line.strip()}")
    c.check("raw reads of job Meta outside JobMeta", not (found),
            "\n    " + "\n    ".join(found))


# ── #289: перерегистрация держит папета на его узле ──────────────────────
# HYPOTHESIS: в спеке только ограничения по проекту и памяти, узел не
# назван; `mop update` (и лечение update) регистрирует джоб заново, и Nomad
# размещает его по свободной памяти. 26.09 pu-mop-6 уехал с hyper на gpu,
# тело с клоном и разговором осталось на hyper бесхозным (так же pu-mop-4/5).
# SOLUTION: node_constraint(узел) -- ${node.unique.name} = узел; job_spec и
# respec берут node и добавляют его к ограничениям; без узла спека та же,
# что и была (слепок не меняется), и версия шаблона от узла не зависит.
# STATUS: FIXED — see #289
def check_node_pin_289(c):
    from mop.common.domain import JobMeta
    pin = getattr(spec, "node_constraint", None)
    if not c.check("spec.node_constraint exists", pin is not None):
        return
    c.expect("node_constraint('hyper')", pin("hyper"),
             {"LTarget": "${node.unique.name}", "Operand": "=", "RTarget": "hyper"})
    bare = spec.job_spec("pu-mop-1", ORIGIN)["Job"]
    pinned = spec.job_spec("pu-mop-1", ORIGIN, node="hyper")["Job"]
    c.expect("without a node the constraints are the project's and the memory's only",
             sorted(x["LTarget"] for x in bare["Constraints"]),
             ["${meta.mop_mem_cap_mb}", "${meta.mop_projects}"])
    c.expect("with a node exactly one constraint is added, the node's",
             [x for x in pinned["Constraints"] if x not in bare["Constraints"]],
             [pin("hyper")])
    c.expect("the node does not change the template version",
             pinned["Meta"].get("mop_spec"), bare["Meta"].get("mop_spec"))
    meta = JobMeta(ORIGIN, "claude", "swarm")
    c.expect("respec passes the node through",
             [x for x in spec.respec("pu-mop-1", meta, node="gpu")["Job"]["Constraints"]
              if x.get("LTarget") == "${node.unique.name}"], [pin("gpu")])
    c.check("respec without a node adds no pin",
            not any(x.get("LTarget") == "${node.unique.name}"
                    for x in spec.respec("pu-mop-1", meta)["Job"]["Constraints"]))


def main():
    if sys.argv[1:] == ["--snapshot"]:
        # Снять слепок заново: только осознанно, когда спека меняется нарочно
        # и все папеты всё равно перерегистрируются.
        with open(SNAPSHOT, "w") as f:
            json.dump({" ".join(str(x) for x in i): render(*i) for i in SPEC_INPUTS},
                      f, ensure_ascii=False, indent=1)
            f.write("\n")
        return 0
    c = Checks()
    for fn in (check_snapshot, check_template_version, check_wrapper_paths,
               check_git_identity, check_driver_free_183,
               check_memory_197, check_project_asks_197, check_spec_memory_197,
               check_nomad_order_197, check_claude_hooks_223, check_job_meta_265,
               check_node_pin_289):
        fn(c)

    for what, j, want in STALE:
        c.check(f"{what}: спека "
                f"{'признана устаревшей' if not want else 'признана свежей'}, "
                f"ждали обратного", not (spec.spec_is_stale(j) != want))

    for project, meta, want in CASES:
        c.expect(f"project {project!r} on a node announcing {meta!r}",
                 serves(project, meta), want)

    # Ограничение обязано быть в каждой спеке: папет без него садится куда
    # угодно, и вся проверка становится украшением.
    c.check("a job spec without the project constraint schedules anywhere",
            not (project_constraint(spec.job_spec("pu-mop-1", ORIGIN)) is None))

    # Проект берётся из ORIGIN, а не из имени: имя — производное, и разойтись
    # они могут только при ручной регистрации, где ошибка и опаснее всего.
    con = project_constraint(spec.job_spec("pu-anything-7", ORIGIN))
    c.check("the constraint must follow the origin's project, not the name",
            not (not re.search(con["RTarget"], "mop")))

    # Врапер -- шелл в base64 спеки, и синтаксическая ошибка в нём видна
    # только на узле, падением каждого подъёма. bash -n ловит её здесь.
    import subprocess
    r = subprocess.run(["bash", "-n"], input=spec.WRAPPER, text=True,
                       capture_output=True)
    c.check("the wrapper is not valid bash", not (r.returncode), r.stderr.strip())

    # ── #256: ветка мастера едет метой джоба, не окружением ───────────────
    # HYPOTHESIS: свежий папет стоит на origin/HEAD (beta3.1 у rudesktop):
    # ветка человека (#249) до регистрации не доезжает. SOLUTION: job_spec
    # берёт branch и кладёт в Meta -- Nomad отдаёт её задаче как
    # NOMAD_META_branch, и `mop driver run` делает checkout после свежего
    # клона. Именно метой, а не Env: окружение задачи и врапер не меняются,
    # версия шаблона та же, ни одна зарегистрированная спека не устаревает.
    # STATUS: FIXED — see #256
    with_b = spec.job_spec("pu-mop-1", ORIGIN, "claude", branch="swarm")["Job"]
    without = spec.job_spec("pu-mop-1", ORIGIN, "claude")["Job"]
    c.check("#256 branch must ride in Meta only when given",
            not (with_b["Meta"].get("branch") != "swarm" or "branch" in without["Meta"]),
            f"{with_b['Meta']} / {without['Meta']}")
    env_b = with_b["TaskGroups"][0]["Tasks"][0]["Env"]
    env_0 = without["TaskGroups"][0]["Tasks"][0]["Env"]
    c.check("#256 the branch must not touch the task env or the template version",
            not (env_b != env_0 or with_b["Meta"][spec.SPEC_META] != without["Meta"][spec.SPEC_META]
                 or spec.spec_is_stale(with_b)))

    return c.report("spec")

if __name__ == "__main__":
    sys.exit(main())
