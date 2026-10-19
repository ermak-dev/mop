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
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
    # Публичный origin (#557): дефолт MOP_PROXY_URL пуст, окружение тел
    # собирается из этих двух -- без пина слепок зависел бы от машины.
    "MOP_SERVER_LAN": "srv.snapshot.test",
    "MOP_HTTPS_PORT": "8443",
    "MOP_LLM_PROXY_PORT": "8317",
}
os.environ.update(PINNED)
time.time = lambda: 1_790_000_000.0
SNAPSHOT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "spec_snapshot.json")

from mop.common import config, llm  # noqa: E402
from mop.server import spec  # noqa: E402
from mop import session  # noqa: E402

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
    versions = {(spec.job_spec(n, o, cont=cont)["Job"].get("Meta") or {}).get("mop_spec")
                for n, o, cont in SPEC_INPUTS}
    c.check("the template version must be one for every puppet",
            not (len(versions) != 1 or None in versions), versions)
    # Одна строка врапера — другая версия: иначе правка врапера снова
    # прошла бы мимо doctor.
    with patched(spec, WRAPPER=spec.WRAPPER + "\n# one more line\n"):
        other = (spec.job_spec(*SPEC_INPUTS[0][:2])["Job"].get("Meta") or {}).get("mop_spec")
    c.check("a changed wrapper line must change the template version",
            not (other in versions or other is None))
    # Версия не зависит от реестра профилей: удалённый MOP_DEFAULT_LLM иначе
    # ронял spec_is_stale, а ростер глотал падение как «спека свежая» —
    # ровно та тихая ошибка, которую закрывает #174.
    c.check("current_version must equal the version job_spec puts in Meta",
            not (spec.current_version() not in versions))
    # HYPOTHESIS (#449): окружение LLM папета едет ЗНАЧЕНИЕМ (PU_LLM_ENV), а
    # версия хешировала только набор ключей -- правка окружения не двигала
    # mop_spec, и стоящий папет со старой картой читался бы свежим вечно
    # (класс #174). SOLUTION: в версию входит то, что реально едет папету.
    # RESULT: правка едущего (адрес прокси) двигает версию; правка того, что
    # спека сбрасывает (слот модели из llm.env()), -- нет: до папета она не
    # доходит, и перерегистрация из-за неё не нужна.
    _today = dict(llm.env())
    with patched(llm, env=lambda: dict(_today, ANTHROPIC_BASE_URL="http://other:1")):
        moved = (spec.job_spec(*SPEC_INPUTS[0][:2])["Job"].get("Meta") or {}).get("mop_spec")
    with patched(llm, env=lambda: dict(_today, ANTHROPIC_DEFAULT_OPUS_MODEL="other-9[1m]")):
        stayed = (spec.job_spec(*SPEC_INPUTS[0][:2])["Job"].get("Meta") or {}).get("mop_spec")
    c.check("a changed riding LLM env must change the template version (#449)",
            not (moved in versions or moved is None), moved)
    c.check("a model slot the spec drops must not move the template version (#449)",
            not (stayed not in versions), stayed)


# (имя, origin, cont): проект с точкой в имени (экранирование в ограничении)
# и разовый подъём с историей. Драйвера среди входов нет: спека от него не
# зависит — внешний врапер один на все тела (docs/DRIVER.md). Профилей
# больше нет (#390): окружение LLM одно. Ролевой вход (#583) -- отдельным
# списком (ROLE_RENDER ниже): render_role зовётся с тройкой, тройка --
# у #583-х помощников глубже по файлу.
SPEC_INPUTS = [
    ("pu-mop-1", "git@git.example.dev:someone/mop.git", False),
    ("pu-mop-2", "git@git.example.dev:someone/mop.git", False),
    ("pu-rugent-3", "https://git.example.dev/rugent/rugent.git", False),
    ("pu-my.proj-1", "git@git.example.dev:someone/my.proj.git", True),
]


def puppet_llm_env(task_environment):
    """Окружение LLM, которое спека квашит папету: PU_LLM_ENV -> словарь."""
    blob = task_environment["PU_LLM_ENV"]
    return dict(kv.split("=", 1) for kv in base64.b64decode(blob).decode().splitlines())


# ── #449: модели папета — каталог прокси, не карта из кода ────────────────
# HYPOTHESIS: спека квасила llm.env() как есть, и статическая карта
# ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL навязывала папету glm-имена,
# которых за прокси нет (каждый ход -- 400 unknown provider), пока мастер
# выбирал модели из живого каталога. SOLUTION: спека собирает окружение
# папета из llm.env() сама: слоты моделей сбрасывает, discovery включает --
# папет выбирает из того же каталога /v1/models, что видит мастер. Мастерская
# сессия (cli/core/_common.py) остаётся на llm.env() как есть: её модель
# выбирает человек, и карта там -- вопрос другого слоя.
# STATUS: FIXED — see #449
DISCOVERY = "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"


def check_llm_env_449(c):
    """Окружение LLM, которое едет папету (#449): каталог, не карта."""
    what = "puppet llm env, "
    for name, origin, _ in SPEC_INPUTS:
        env = spec.job_spec(name, origin)["Job"]["TaskGroups"][0]["Tasks"][0]["Env"]
        baked = puppet_llm_env(env)
        # (a) ни одного навязанного слота модели: имена выбирает каталог
        slots = sorted(k for k in baked if k.startswith("ANTHROPIC_DEFAULT_"))
        c.check(f"{what}no model slot rides to {name}", not slots, slots)
        # (b) каталог включён: без него claude звал бы дефолтные имена,
        # которых в прокси нет -- тот же 400, только позже
        c.check(f"{what}discovery is on for {name}",
                baked.get(DISCOVERY) == "1", baked.get(DISCOVERY))
        # (c) адрес и таймаут -- из llm.env(), ключ -- по-прежнему на узле
        c.check(f"{what}the proxy URL and timeout ride unchanged to {name}",
                (baked.get("ANTHROPIC_BASE_URL"), baked.get("API_TIMEOUT_MS"))
                == (llm.env()["ANTHROPIC_BASE_URL"], llm.env()["API_TIMEOUT_MS"]), baked)
        c.check(f"{what}the key still comes from the node for {name}",
                (env.get("PU_LLM_KEY_VAR"), env.get("PU_LLM_AUTH_VAR")),
                (llm.KEY, llm.AUTH_VAR))
        # (d) карта llm.env() НЕ едет папету целиком: спека -- тот слой,
        # который решает, что из неё положено папету
        _map = dict(llm.env())
        with patched(llm, env=lambda: dict(_map, ANTHROPIC_DEFAULT_OPUS_MODEL="gone-9")):
            c.check(f"{what}a model slot from llm.env() is dropped for {name}",
                    "ANTHROPIC_DEFAULT_OPUS_MODEL" not in puppet_llm_env(
                        spec.job_spec(name, origin)["Job"]["TaskGroups"][0]["Tasks"][0]["Env"]))


def render(name, origin, cont):
    """Спека ровно так, как её увидит Nomad: JSON, в порядке ключей."""
    return json.dumps(spec.job_spec(name, origin, cont=cont),
                      ensure_ascii=False, indent=1)


# Ролевой вход слепка (#583): тройка из поставляемой роли (mop/skills) --
# стабильный digest реального бандла, едет в спеку и в слепок байт в байт.
# Бандл обязан лежать в хранилище сервера (#582: закрепление до джоба), для
# слепка его сеет seed_role_store.
ROLE_RENDER = ("pu-mop-9", "git@git.example.dev:someone/mop.git", False)


def seed_role_store(role="puppet"):
    """Хранилище бандлов с одной закреплённой ролью: тот же посев, что у
    tests/soul_lifecycle.py (#582), -- grants.prepare пишет бандл
    write-once, спека без него отказывает. -> (корень, digest)."""
    import yaml
    from mop.server import grants
    manifest, prompt, digest = role_triple(role)
    root = tempfile.mkdtemp(prefix="mop-583-store-")
    grants.prepare(project_of(ROLE_RENDER[1]), role=role, digest=digest,
                   manifest=manifest, prompt=prompt, owner="snapshot",
                   request_id=f"seed-{digest[:8]}", job=ROLE_RENDER[0],
                   soul_inc=INC, intent=f"seed-{digest[:8]}", root=root)
    return root, digest


def project_of(origin):
    """Имя проекта тем же правилом, что спека (driver.project_of)."""
    from mop import driver
    return driver.project_of(origin)


def render_role(name, origin, cont, store=None):
    """Ролевая спека -- так же в Nomad: с тройкой роли (#583). Сторы --
    параметром проверки, слепок сеет свой."""
    manifest, prompt, digest = role_triple("puppet")
    with patched(spec, SOUL_STORE=store or seed_role_store()[0]):
        return json.dumps(spec.job_spec(name, origin, cont=cont,
                                        soul=manifest["soul"],
                                        soul_digest=digest, soul_inc=INC),
                          ensure_ascii=False, indent=1)


# ── #557: корень сервера в теле -- из той же раздачи, что и ключ ─────────
CA_BEGIN = "# --- корень сервера для TLS (#557) ---"
CA_END = "# --- конец корня сервера (#557) ---"


def ca_section():
    """Кусок врапера про корень TLS -- ровно тот текст, что едет."""
    w = spec.WRAPPER
    if CA_BEGIN not in w or CA_END not in w:
        return None
    return w[w.index(CA_BEGIN):w.index(CA_END)]


def run_ca_section(section, home, secrets):
    """Кусок корня -- как в теле: set -e, свой HOME, secrets.env узла
    (пишет ВЫЗЫВАЮЩИЙ: проверка сама решает, есть ли в нём строка корня).

    -> процесс, чей stdout -- пары var=value окружения claude по строке
    (то, что врапер положил бы в llm_env), и в конце sentinel."""
    import subprocess
    script = ("set -e\nkeyfile=" + repr(secrets) + "\nllm_env=()\n" + section
              + '\nfor kv in "${llm_env[@]}"; do printf "%s\\n" "$kv"; done\n'
              + 'printf "DONE\\n"\n')
    return subprocess.run(["/bin/bash", "-c", script], capture_output=True,
                          text=True, env={"HOME": home, "PATH": os.environ["PATH"]})


def check_ca_section_557(c):
    """Врапер кладёт корень сервера в тело и даёт claude NODE_EXTRA_CA_CERTS.

    Строка MOP_PROXY_CA (base64 PEM) в secrets.env узла декодируется в файл
    под $HOME тела и едет переменной NODE_EXTRA_CA_CERTS; сломанная строка --
    громкий выход (как отсутствие ключа); строки нет -- системное доверие."""
    import base64
    import shutil
    import tempfile
    section = ca_section()
    if not c.check("#557 the wrapper has a CA section",
                   section is not None, "no markers in spec.WRAPPER"):
        return
    pem = "-----BEGIN CERTIFICATE-----\nroot\n-----END CERTIFICATE-----\n"
    line = base64.b64encode(pem.encode()).decode()
    home = tempfile.mkdtemp(prefix="mop-557-body-")
    try:
        secrets = os.path.join(home, "secrets.env")
        # (a) строка есть: файл декодирован, переменная указывает на него.
        with open(secrets, "w") as f:
            f.write(f"MOP_PROXY_KEY=k1\nMOP_PROXY_CA={line}\n")
        r = run_ca_section(section, home, secrets)
        if not c.check("#557 the CA section succeeds", not r.returncode,
                       r.stderr.strip()):
            return
        got = dict(kv.split("=", 1) for kv in r.stdout.splitlines() if "=" in kv)
        ca_file = os.path.join(home, ".config/mop/tls.pem")
        c.check("#557 NODE_EXTRA_CA_CERTS names the decoded file",
                got.get("NODE_EXTRA_CA_CERTS") == ca_file, got)
        c.check("#557 the root is decoded into the body",
                os.path.isfile(ca_file) and open(ca_file).read() == pem,
                ca_file if os.path.isfile(ca_file) else "missing")
        # (b) сломанная строка: громкий выход, файл-мусор не в счёт.
        with open(secrets, "w") as f:
            f.write("MOP_PROXY_KEY=k1\nMOP_PROXY_CA=@@not-base64@@\n")
        r = run_ca_section(section, home, secrets)
        c.check("#557 a broken CA line fails loudly",
                r.returncode != 0 and "MOP_PROXY_CA" in r.stderr,
                (r.returncode, r.stderr.strip()))
        # (c) строки нет: системное доверие, переменной нет.
        with open(secrets, "w") as f:
            f.write("MOP_PROXY_KEY=k1\n")
        r = run_ca_section(section, home, secrets)
        if c.check("#557 no CA line keeps the section quiet", not r.returncode,
                   r.stderr.strip()):
            got = [kv for kv in r.stdout.splitlines() if kv.startswith("NODE_EXTRA")]
            c.check("#557 without the line there is no NODE_EXTRA_CA_CERTS",
                    not got, got)
    finally:
        shutil.rmtree(home, ignore_errors=True)


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
    # Ролевой случай (#583): свой ключ, тройка в Meta и PU_SOUL* в окружении
    key = " ".join(str(x) for x in ROLE_RENDER) + " role"
    if c.check(f"rendered role spec for {key} differs from the snapshot",
               not (render_role(*ROLE_RENDER) != want.get(key))):
        job = json.loads(want[key])["Job"]
        c.check("the snapshot role spec reads as stale",
                not (spec.spec_is_stale(job)
                     or job["Meta"].get("soul") != "puppet"
                     or "PU_SOUL_DIR" not in job["TaskGroups"][0]["Tasks"][0]["Env"]))
    c.check("the snapshot and SPEC_INPUTS disagree on the cases",
            not (set(want) != {" ".join(str(x) for x in i) for i in SPEC_INPUTS}
                 | {key}))


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
    for name, origin, cont in SPEC_INPUTS:
        env = spec.job_spec(name, origin, cont=cont)["Job"]["TaskGroups"][0]["Tasks"][0]["Env"]
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
        # PU_NODE -- исключение по договору (#583/#590): врапер его читает,
        # но даёт его телу лаунчер из кредов узла, не спека -- Nomad
        # выбирает узел после регистрации, спеке имя узла неизвестно.
        # Ролевые ключи (#583) у легаси-входов отсутствуют по той же
        # причине, что и в спеке: их кладёт task_env тройки ради, а врапер
        # читает ровно на ролевой ветке -- контракт ролевого окружения
        # проверяет check_role_startup_583.
        reads = set(re.findall(r"\$\{?(PU_[A-Z_]+)", spec.WRAPPER))
        missing = sorted(reads - set(body) - {"PU_NODE"}
                         - {k for k in reads if k.startswith("PU_SOUL")})
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
        env = spec.job_spec(*inputs[:2])["Job"]["TaskGroups"][0]["Tasks"][0]["Env"]
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
# События -- те, что разбирает session.py, а не третья копия списка (#341).
HOOK_EVENTS = set(session.CLEARING) | {session.FAILURE}
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



def wrapper_hook_events(job):
    """События, на которые врапер из спеки ставит хуки: массив, который
    jq превращает в ключи объекта hooks. Без jq: разбор текста врапера."""
    import base64
    env = job["Job"]["TaskGroups"][0]["Tasks"][0]["Env"]
    wrapper = base64.b64decode(env["PU_WRAPPER"]).decode()
    m = re.search(r"\{hooks: \((\[[^\]]*\])\s*\|\s*map\(", wrapper)
    return json.loads(m.group(1)) if m else None


def check_hook_events_341(c):
    """#341: врапер ставит хуки списком литералов, а session.py разбирает
    исход хода по CLEARING и FAILURE. Новое событие в session.py без правки
    врапера в теле не сработало бы никогда, и молча. Порядок не важен: jq
    собирает из списка объект (from_entries). Врапер не правится -- правка
    перерегистрирует каждый джоб; связь держит эта проверка."""
    want = HOOK_EVENTS
    for inputs in SPEC_INPUTS:
        got = wrapper_hook_events(spec.job_spec(*inputs[:2], cont=inputs[2]))
        c.check(f"{inputs[0]}: the wrapper hooks exactly session's events",
                got is not None and len(got) == len(set(got)) and set(got) == want,
                f"wrapper {got}, session {sorted(want)}")

# ── #455: статус-строка -- в тот же файл --settings, тем же корнем ────────
# HYPOTHESIS: файл --settings врапера несёт только хуки, и статус-строка
# папета не настроена вовсе: claude показывает свой дефолт, который ничего
# не знает о пуле. Глагол statusline (#453) читает stdin JSON и печатает
# строку, но claude папета его не зовёт -- ровно та же болезнь, что #223
# с хуками.
# SOLUTION: тот же jq, который пишет хуки, кладёт рядом top-level statusLine
# с командой `python3 <тот же mop_root>/mop/session.py statusline` (#223:
# корень -- тот же, откуда стартует mop, иначе команда молча умрёт на узле).
# Охрана своя, не копия хуковой: stdout хука -- шум в контексте модели, а
# stdout статус-строки -- САМА строка, глушить её значило бы убрать
# статус-строку целиком. Гасятся stderr и код выхода: старый session.py без
# глагола печатает usage в stderr и выходит 2, отсутствующий -- ненулевой,
# и без охраны это видит экран папета на каждом ходу.
# STATUS: RED
STATUSLINE_VERB = "statusline"
STATUSLINE_GUARD = "2>/dev/null || true"


def check_statusline_455(c):
    """#455: файл --settings несёт хуки как прежде И top-level statusLine
    командой из того же mop_root; охрана гасит stderr и код выхода любого
    session.py на узле, не трогая stdout."""
    import shutil
    import subprocess
    import tempfile
    section = hooks_section()
    if not c.check("the wrapper has no claude hooks section", not (section is None)):
        return
    if shutil.which("jq", path=os.environ["PATH"]) is None:
        hermetic.skip("the statusline settings check", "no jq on this machine")
        return
    home = tempfile.mkdtemp()
    try:
        # Кусок исполняется как в теле: set -e, свой HOME, имя папета.
        r = run_hooks_section(section, home, os.environ["PATH"])
        if not c.check("the hooks section fails", not (r.returncode), r.stderr.strip()):
            return
        mop_root, hooks, _settings = r.stdout.split("\n")
        try:
            with open(hooks) as f:
                doc = json.load(f)
        except (OSError, ValueError) as e:
            c.fail("the settings file is not JSON", e)
            return
        # (a) хуки как прежде: пять событий, каждая -- охраняемый вызов hook
        events = (doc.get("hooks") or {})
        cmds = {h.get("command") for groups in events.values()
                for g in groups for h in g.get("hooks", [])}
        session_py = f"{mop_root}/mop/session.py"
        c.check("the hooks must stay as they were, on the same session.py",
                not (set(events) != HOOK_EVENTS
                     or cmds != {f"python3 {session_py} hook >/dev/null 2>&1 || true"}),
                f"{sorted(events)}, {sorted(cmds)}")
        # (b) top-level statusLine: глагол того же session.py, из того же
        # mop_root, с охраной stderr и кода выхода
        line = doc.get("statusLine")
        c.check("the settings must carry a top-level statusLine command",
                isinstance(line, dict) and line.get("type") == "command"
                and isinstance(line.get("command"), str), line)
        cmd = line.get("command", "") if isinstance(line, dict) else ""
        want = f"python3 {session_py} {STATUSLINE_VERB} {STATUSLINE_GUARD}"
        c.check(f"the statusLine command must be {STATUSLINE_VERB!r} of the "
                "same session.py, guarded",
                cmd == want, f"got {cmd!r}, want {want!r}")
        # (c) охрана держит против любого session.py на узле: старый без
        # глагола (usage в stderr, выход 2) и отсутствующий. Выход 0 и тишина
        # в stderr; stdout -- полезная нагрузка, от старого её просто нет.
        os.makedirs(os.path.dirname(session_py), exist_ok=True)
        for what, body in (("an old session.py", "import sys\n"
                            "print('usage: session.py ...', file=sys.stderr)\nsys.exit(2)\n"),
                           ("no session.py", None)):
            if body is None:
                os.remove(session_py)
            else:
                with open(session_py, "w") as f:
                    f.write(body)
            r = subprocess.run(["sh", "-c", cmd], input="{}", text=True,
                               capture_output=True)
            c.check(f"{what}: the statusLine command must exit 0 with no stderr",
                    not (r.returncode or r.stderr),
                    f"got {r.returncode} {r.stderr!r}")
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
    # (e) claude стартует с этим файлом, когда он есть. Аргументы собираются
    # массивом (#583, ревью: имя файла промпта с пробелом ломало склеенную
    # строку), поэтому и проверка смотрит массив: файл хуков -- элемент argv
    # парой (--settings, путь), и массив доезжает до команды панели.
    panel = spec.WRAPPER.split('tmux -L "$PU_NAME" new-session')[-1]
    c.check("the claude launch line must carry the settings argv",
            not ('claude_argv+=("--settings" "$hooks")' not in spec.WRAPPER
                 or not re.search(r'^claude_argv=\(', spec.WRAPPER, re.M)
                 or '"${claude_argv[@]}"' not in panel),
            "no --settings element in the claude argv array")
    # (f) врапер едет base64 (#155): доллары одинарные, а в открытой части
    # спеки нет ни JSON хуков, ни `${…}`, кроме меты узла.
    c.check("the wrapper travels base64: a $$ in it would reach the body literally",
            not ("$$" in spec.WRAPPER))
    for inputs in SPEC_INPUTS:
        job = spec.job_spec(*inputs[:2], cont=inputs[2])
        env = job["Job"]["TaskGroups"][0]["Tasks"][0]["Env"]
        c.check(f"{inputs[0]}: the wrapper in the spec has no hooks",
                not ('claude_argv+=("--settings" "$hooks")' not in
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
BRANCHED = ("pu-mop-3", "git@git.example.dev:someone/mop.git", False, "feat/256-x")


# ── #420: папету нельзя черновики feedback — ни писать, ни подсказывать ────
# HYPOTHESIS: у папета нет человека, которому показать черновик feedback, а
# Claude Code 2.1.287 даёт модели инструмент SendFeedback: она пишет черновики
# в ~/.claude/feedback/drafts/ и заметку о них на хост-узел, где ~/.claude
# делят все claude машины. Инструмент не выключен, политика feedbackDrafts по
# умолчанию notify — и ход за ходом копятся черновики, которых никто не
# отправит, и подсказки «покажи /feedback».
# SOLUTION: врапер переводит политику feedbackDrafts в off (значение из
# Claude Code 2.1.287: notify | quiet | off, restrictive — off) в своём
# ~/.claude/settings.json — не в общем на узле: каждый подъём сходится сам,
# и чужие сессии узла не задеваются; deny-правило на SendFeedback — то же
# место, что SendMessage (#223): инструмента нет и в списке, его не choosing.
# DISABLE_FEEDBACK_COMMAND гасит и /feedback-команду в панели. Ничего больше
# не выключается: Remote Control и автообновления остаются как были.
# STATUS: FIXED — see #420


# Тело edit_json из врапера (#420): секция feedback исполняется с ним же,
# как в теле, без второго парсера bash.
EDIT_JSON = 'edit_json() {  # <file> <jq-program> [jq-args...]\n    local file="$1" program="$2"; shift 2\n    local tmp="$file.$PU_NAME.tmp"\n    exec 9>"$file.lock"\n    flock 9\n    # `jq empty` is not a validity check: a zero-byte file is an empty jq input\n    # stream, so it exits 0, every filter over it yields nothing, and the 0-byte\n    # config got written straight back (2026-08-28, one node — all six puppets parked\n    # on the config prompt while `mop list` showed only "HUNG"). Demand an\n    # actual object, and never install an empty result.\n    jq -e \'type == "object"\' "$file" >/dev/null 2>&1 || echo \'{}\' > "$file"\n    if jq "$@" "$program" "$file" > "$tmp" && [ -s "$tmp" ]; then\n        mv "$tmp" "$file"\n    else\n        rm -f "$tmp"\n    fi\n    flock -u 9\n    exec 9>&-\n}'

def check_no_feedback_420(c):
    import tempfile
    section = re.search(r"^# --- feedback drafts[^\n]*\n(.*?)^# --- end of feedback drafts ---",
                        spec.WRAPPER, re.M | re.S)
    if not c.check("#420 the wrapper has a feedback drafts section",
                   not (section is None)):
        return
    body = section.group(1)
    # (a) политика — off, имя инструмента — то, что видно в 2.1.287
    c.check("#420 feedbackDrafts is turned off",
            not ('feedbackDrafts = "off"' not in body), body)
    c.check("#420 the deny rule carries SendFeedback",
            not ('"SendFeedback"' not in body), body)
    c.check("#420 the /feedback command is disabled",
            not ("DISABLE_FEEDBACK_COMMAND=1" not in body), body)
    # (b) файл узловой, замок и починка — общие edit_json; чужого не трогаем
    c.check("#420 the policy goes to the node-level settings.json",
            not ("edit_json" not in body
                 or "$HOME/.claude/settings.json" not in body), body)
    c.check("#420 no other policy is touched",
            not (re.search(r"\b(skipDangerousModePermissionPrompt|RemoteControl|"
                           r"autoUpdate|auto_updates)\b", body) is not None), body)
    # (c) секция исполняется как есть: свой HOME, замок и jq как в теле
    home = tempfile.mkdtemp()
    try:
        # Узловой settings.json общий для всех папетов узла: секция обязана
        # вливаться, а не присваивать. Стартовый файл -- с чужими deny-правилами
        # и посторонним ключом; секция гоняется дважды (каждый подъём).
        seed = {"permissions": {"deny": ["SendMessage", "AskUserQuestion"]},
                "editorMode": "vim"}
        os.makedirs(os.path.join(home, ".claude"))
        with open(os.path.join(home, ".claude", "settings.json"), "w") as f:
            json.dump(seed, f)
        script = ("set -e\n" + EDIT_JSON + "\n"
                  + body + '\nprintf "%s" "$DISABLE_FEEDBACK_COMMAND"\n')
        env = {"HOME": home, "PU_NAME": "pu-mop-1", "PATH": os.environ["PATH"]}
        for _ in range(2):
            r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, env=env)
        if c.check("#420 the feedback section runs clean",
                   not (r.returncode or r.stderr.strip()),
                   f"exit {r.returncode}: {r.stderr.strip()[-200:]}"):
            with open(os.path.join(home, ".claude", "settings.json")) as f:
                doc = json.load(f)
            c.expect("#420 the written settings carry the policy",
                     doc.get("feedbackDrafts"), "off")
            deny = (doc.get("permissions") or {}).get("deny", [])
            c.check("#420 the foreign deny rules survive",
                    not (not {"SendMessage", "AskUserQuestion"} <= set(deny)), deny)
            c.expect("#420 SendFeedback lands in deny exactly once",
                     deny.count("SendFeedback"), 1)
            c.expect("#420 a foreign top-level key survives",
                     doc.get("editorMode"), "vim")
    finally:
        shutil.rmtree(home)


def check_job_meta_265(c):
    from mop.common import domain
    JobMeta = getattr(domain, "JobMeta", None)
    c.check("mop.common.domain has no JobMeta", not (JobMeta is None))
    # Круг без потерь, байт в байт и в том же порядке ключей.
    for name, origin, cont, *branch in (SPEC_INPUTS + [BRANCHED]) if JobMeta else []:
        meta = spec.job_spec(name, origin, cont=cont,
                             branch=branch[0] if branch else None)["Job"]["Meta"]
        m = JobMeta.from_meta(meta)
        back = m.to_meta()
        c.check(f"JobMeta round trip for {name}", not (json.dumps(back) != json.dumps(meta)),
                f"{back} != {meta}")
        c.check(f"from_job and from_meta disagree for {name}",
                not (JobMeta.from_job({"Meta": meta}) != m))
        c.check(f"JobMeta.project for {name}",
                not (m.project != spec.driver.project_of(origin)), repr(m.project))
    # Аренда кредита выпилена (#384): в Meta -- origin, llm, ветка, версия.
    c.check("no cred or llm keys in Meta any more (#384, #390)",
            not {"cred", "llm"} & set(spec.job_spec(*BRANCHED[:2], branch=BRANCHED[3])["Job"]["Meta"]))
    empty = JobMeta.from_job({}) if JobMeta else None
    c.check("a job without Meta",
            not (empty is not None and (empty.origin, empty.branch, empty.spec_version,
                                        empty.project) != (None, None, None, "")), empty)
    if JobMeta is not None:
        try:
            JobMeta.from_meta({"origin": "o"}).__setattr__("branch", "x")
            c.fail("JobMeta must be frozen")
        except AttributeError:
            pass

    # Сборка образа снимает папетов и поднимает их с веткой мастера.
    from mop.server import image
    from mop.common import puppets
    job = spec.job_spec(*BRANCHED[:2], branch=BRANCHED[3])["Job"]
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
            not (got != [BRANCHED[3]]), f"job_spec calls {called}")

    # Лечение doctor'а (update) несёт ветку папета.
    sent = []
    with patched(puppets, _cluster=lambda verb, **kw: sent.append((verb, kw)) or (
            {"meta": job["Meta"]} if verb == "spec" else {})):
        puppets.treat({"action": "update", "alloc": None, "name": job["ID"]})
    upd = [kw for verb, kw in sent if verb == "update"]
    c.check("treat must re-register with the puppet's branch",
            not (not upd or upd[0].get("branch") != BRANCHED[3]
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


# ── #583: общий ролевой старт обёртки ──────────────────────────────────────
# HYPOTHESIS: обёртка не знает про роли. Тройка роли едет в Meta (#582), а
# до тела доезжает только каталог инкарнации (#590 кладёт бандл, кред
# принципала и расписку готовности) -- обёртка его не читает и подняла бы
# ролевого папета без промпта роли, на проектном креде и с общими на узел
# настройками claude. Всё, против чего эпик (#579: роль без бандла не
# стартует, расхождение -- громкий отказ на старте).
# SOLUTION: тройка едет окружением спеки (PU_SOUL/PU_SOUL_DIGEST/PU_SOUL_INC
# + PU_SOUL_DIR из driver.soul_dir), обёртка ДО первого шага с телом
# сверяет расписку готовости и кред с метой джоба, перечитывает бандл тем
# же правилом, что регистрация (soul.load), и поднимает claude с промптом
# бандла, кредом принципала и своими на папета --settings/--mcp-config.
# Разница ролей -- только данные: ни одной проверки имени роли. Легаси-джоб
# ходит прежним путём без новой ветки; из ролевой ветки легаси-путь
# недостижим -- битый бандл не поднимает «просто папета».
# STATUS: RED
ROLE_BEGIN = "# --- role startup (#583) ---"
ROLE_END = "# --- end of role startup (#583) ---"

INC = "1b4e28ba-2fa1-4d2b-9a5e-3c1f0a7b8c9d"
GRANT = "grant-" + "a1" * 16

# Подмена claude (#583): argv панели и срез окружения сессии -- в журнал по
# MOP_FAKE_LOG; `mcp add` зовется тем же путём и тоже журналяется (его
# наличие -- часть контракта легаси-пути, отсутствие -- ролевого).
FAKE_CLAUDE = """#!/bin/bash
printf '%s\\n' "$*" >> "$MOP_FAKE_LOG"
env | grep -E '^MOP_(SOUL_DIR|PUPPET|NODE|BUS_CONFIG|PROJECT)=' | sort >> "$MOP_FAKE_LOG"
[ "$1" = mcp ] && printf 'mcp %s\\n' "$*" >> "$MOP_FAKE_LOG"
exit 0
"""

# Подмена tmux (#583): new-session исполняет команду панели с её -e
# окружением синхронно, has-session всегда «нет» -- обёртка доходит до
# конца, а журнал claude уже написан. kill-session -- успех.
FAKE_TMUX = """#!/bin/bash
verb=""
for a in "$@"; do
    case "$a" in new-session|kill-session|has-session) verb="$a";; esac
done
case "$verb" in
    new-session)
        envs=()
        prev=""
        for a in "$@"; do
            [ "$prev" = "-e" ] && envs+=("$a")
            prev="$a"
        done
        exec env ${envs[@]+"${envs[@]}"} bash -c "${@: -1}"
        ;;
    kill-session) exit 0;;
    *) exit 1;;
esac
"""

# npx обязан врать быстро (#583): настоящий качал бы chromium из сети.
FAKE_NPX = "#!/bin/sh\nexit 1\n"


def role_triple(which):
    """Поставляемая роль (mop/skills/<роль>) -> (манифест-словарь, промпт,
    digest). Тем же правилом, что сервер: soul.load каталога."""
    from mop.common import soul
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
                     "mop", "skills", which)
    b = soul.load(d)
    return b.manifest.to_dict(), b.prompt, b.digest


def plant_soul(home, name, manifest, prompt, digest,
               ready=None, files=None, creds=None):
    """Разложить каталог инкарнации как его кладёт #590: bundle/ из
    soul.yaml (JSON, как его пишет доставка) и промпта, рядом bus.json и
    ready.json. -> путь корня. Подмены для проверок расхождения: ready --
    False (файла нет) или словарь (чужая расписка); files -- свой набор
    файлов бандла; creds -- False (креда нет). None -- честная раскладка:
    расписка по тройке, кред -- принципал, бандл -- ровно два файла."""
    root = os.path.join(home, ".config", "mop", "souls", name, INC)
    bundle = os.path.join(root, "bundle")
    os.makedirs(bundle, exist_ok=True)
    planted = files if files is not None else {
        "soul.yaml": json.dumps(manifest, sort_keys=True) + "\n",
        manifest["prompt"]: prompt.decode(),
    }
    for fn, text in planted.items():
        with open(os.path.join(bundle, fn), "w") as f:
            f.write(text)
    if creds is not False:
        with open(os.path.join(root, "bus.json"), "w") as f:
            json.dump(creds or {"url": "nats://10.0.0.1:4222",
                                "user": GRANT, "password": "p"}, f)
    if ready is False:
        pass
    else:
        with open(os.path.join(root, "ready.json"), "w") as f:
            json.dump(ready or {"ready": True, "role": manifest["soul"],
                                "soul_inc": INC, "soul_digest": digest}, f)
    return root


def fake_tools(home):
    """Подмены (#583) -> путь каталога на PATH. claude -- в двух местах:
    в теле врапер зовёт его и абсолютным путём ($HOME/.local/bin/claude,
    строка запуска панели), и именем (`claude mcp add`, PATH); подмена
    обязана перехватить оба. tmux/npx -- тем же каталогом."""
    local = os.path.join(home, ".local", "bin")
    d = os.path.join(home, "fake-bin")
    os.makedirs(local)
    os.makedirs(d)
    for name, text in (("claude", FAKE_CLAUDE), ("tmux", FAKE_TMUX),
                       ("npx", FAKE_NPX)):
        for where in (local, d) if name == "claude" else (d,):
            p = os.path.join(where, name)
            with open(p, "w") as f:
                f.write(text)
            os.chmod(p, 0o755)
    return d


def run_wrapper(home, env, timeout=180):
    """Врапер целиком в поддельном теле. -> процесс."""
    import subprocess
    return subprocess.run(["/bin/bash", "-c", spec.WRAPPER],
                          env={**env, "HOME": home}, capture_output=True,
                          text=True, timeout=timeout)


def fake_body(name, which=None, cont=False, plant=None, env_extra=None):
    """Сгенерированное окружение задачи + поддельное тело. -> (env, дом,
    путь журнала claude). Роль -- из mop/skills: манифест и промпт честные,
    раскладка -- plant (по умолчанию честная)."""
    import subprocess
    home = tempfile.mkdtemp(prefix="mop-583-")
    log = os.path.join(home, "claude.log")
    # Пакет в теле ($HOME/mop): ролевые врата импортируют mop.common.soul.
    os.symlink(os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
               os.path.join(home, "mop"))
    tools = fake_tools(home)
    manifest = prompt = digest = None
    if which is not None:
        manifest, prompt, digest = role_triple(which)
        # plant сам знает тройку (случаи отказа закрывают над ней), поэтому
        # договор -- (дом, имя); честная раскладка -- то же самое с тройкой
        # из role_triple.
        if plant:
            plant(home, name)
        else:
            plant_soul(home, name, manifest, prompt, digest)
    env = dict(spec.task_env(name, ORIGIN, cont))
    clone = os.path.join(home, "puppets", name)
    os.makedirs(clone)
    subprocess.run(["git", "init", "-q", clone], check=True)
    subprocess.run(["git", "-C", clone, "remote", "add", "origin",
                    env["PU_ORIGIN"]], check=True)
    creds = os.path.join(home, f"bus-{env['PU_PROJECT']}.json")
    with open(creds, "w") as f:
        json.dump({"url": "nats://10.0.0.1:4222",
                   "user": f"puppet-{env['PU_PROJECT']}", "password": "p"}, f)
    env.update({
        "PU_CLONE": clone,
        "PU_TARGET": os.path.join(home, f"target-{name}"),
        "PU_SECRETS": os.path.join(home, "secrets.env"),
        "PU_PROJECT_CREDS": creds,
        "PU_SECRETS_DIR": os.path.join(home, "secrets"),
        "PU_LLM_ENV": "", "PU_LLM_KEY_VAR": "", "PU_LLM_AUTH_VAR": "",
        "PU_SEED": "",
        "HOME": home, "MOP_FAKE_LOG": log,
        # Подмены -- первыми: путь задачи из спеки несёт {HOME} дома пула,
        # но вызовы по имени (claude mcp add, npx, tmux) обязаны видеть
        # подмены раньше настоящих claude/npx этой машины.
        "PATH": tools + ":" + env["PATH"].replace(
            "{HOME}/.cargo/bin", home + "/.cargo/bin").replace(
            "{HOME}/.nvm", home + "/.nvm").replace("{HOME}", home),
    })
    if which is not None:
        # Ролевые поля -- руками: contract окружения спеки проверяет свой
        # случай ниже, а здесь от него не зависит.
        env.update({"PU_SOUL": manifest["soul"], "PU_SOUL_DIGEST": digest,
                    "PU_SOUL_INC": INC,
                    "PU_SOUL_DIR": os.path.join(
                        home, ".config", "mop", "souls", name, INC),
                    "PU_NODE": "node-a"})
    if env_extra:
        env.update(env_extra)
    return env, home, log


def launch_record(log):
    """Журнал подмены claude -> (строка argv панели, {переменная:
    значение}); без журнала -- (None, {}): модель не стартовала. Первая
    строка журнала -- запуск панели: `mcp add` пишется в конец, после
    строк окружения."""
    if not os.path.exists(log):
        return None, {}
    lines = open(log).read().splitlines()
    panel = next((l for l in lines if not l.startswith("MOP_")
                  and not l.startswith("mcp ")), None)
    env = dict(l.split("=", 1) for l in lines
               if l.startswith("MOP_") and "=" in l)
    return panel, env


def role_section():
    """Кусок ролевого старта -- ровно тот текст, что едет."""
    w = spec.WRAPPER
    if ROLE_BEGIN not in w or ROLE_END not in w:
        return None
    return w[w.index(ROLE_BEGIN):w.index(ROLE_END)]


def check_role_startup_583(c):
    import shutil
    import subprocess
    import tempfile
    from mop import driver
    from mop.common import soul  # noqa: F401 -- бандл-правило теста через role_triple

    # (0) контракт окружения спеки: тройка -> PU_SOUL*, путь -- driver.soul_dir
    manifest, prompt, digest = role_triple("puppet")
    try:
        role = spec.task_env("pu-mop-1", ORIGIN, False,
                             soul=manifest["soul"], soul_digest=digest,
                             soul_inc=INC)
        bad = None
    except TypeError as e:
        role, bad = None, e
    legacy = spec.task_env("pu-mop-1", ORIGIN, False)
    if c.check("#583 task_env takes the role triple", not bad, bad):
        for key, want in (("PU_SOUL", manifest["soul"]),
                          ("PU_SOUL_DIGEST", digest), ("PU_SOUL_INC", INC),
                          ("PU_SOUL_DIR", driver.soul_dir("pu-mop-1", INC))):
            c.check(f"#583 {key} rides to the body",
                    not (role.get(key) != want), f"{role.get(key)!r} != {want!r}")
        c.check("#583 the role triple rides in PU_CARRY",
                not (set(role["PU_CARRY"].split(",")) <
                     {"PU_SOUL", "PU_SOUL_DIGEST", "PU_SOUL_INC", "PU_SOUL_DIR"}),
                role.get("PU_CARRY"))
        c.check("#583 the role fills exactly the PU_SOUL transport keys",
                not (set(role) != set(legacy)
                     or sorted(k for k in role if k.startswith("PU_SOUL"))
                     != ["PU_SOUL", "PU_SOUL_DIGEST", "PU_SOUL_DIR",
                         "PU_SOUL_INC"]),
                sorted(set(role) ^ set(legacy)))
        c.check("#583 the role env carries no dollar and no secret",
                not any("$" in v or "password" in v.lower()
                        for v in role.values()), sorted(role))
        c.check("#583 the role must not move the template version",
                not (spec.template_version(role) != spec.template_version(legacy)),
                "role keys are the puppet's own data, like its name")
    c.check("#583 task_env carries no node name: the node is the launcher's to name",
            not ("PU_NODE" in legacy or (role is not None and "PU_NODE" in role)),
            "PU_NODE comes from #590")

    section = role_section()

    # (1) легаси-джоб: прежний путь, ни одной новой ветки
    env, home, log = fake_body("pu-mop-1")
    try:
        r = run_wrapper(home, env)
        argv, senv = launch_record(log)
        if c.check("#583 a legacy job still launches claude",
                   not (r.returncode or argv is None),
                   f"exit {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}"):
            c.check("#583 the legacy claude carries no role prompt flag",
                    not ("--append-system-prompt" in argv), argv)
            c.check("#583 the legacy claude keeps the shared hooks file",
                    not ("--settings " not in argv
                         or f"{home}/.config/mop/claude-hooks.json" not in argv),
                    argv)
            c.check("#583 the legacy claude rides the project credential",
                    not (senv.get("MOP_BUS_CONFIG") != env["PU_PROJECT_CREDS"]
                         or "MOP_SOUL_DIR" in senv or "MOP_PUPPET" in senv),
                    senv)
            c.check("#583 the legacy path registers mcp through claude itself",
                    not ("mcp add --scope user mop" not in open(log).read()),
                    "no `claude mcp add` in the journal")
    finally:
        shutil.rmtree(home)

    if not c.check("#583 the wrapper has a role startup section",
                   not (section is None)):
        return
    code = "\n".join(l for l in section.splitlines()
                     if not l.lstrip().startswith("#"))
    c.check("#583 the role branch is data, no role name is checked",
            not re.search(r"\b(master|builder|puppet)\b", code),
            re.findall(r"\b(?:master|builder|puppet)\b", code))
    c.check("#583 the gate heredoc is quoted: no shell expansion of python",
            not ("<<'PY'" not in section))

    # (2) роль: старт с промптом бандла, своим кредом и своими настройками
    env, home, log = fake_body("pu-mop-1", "puppet")
    try:
        r = run_wrapper(home, env)
        argv, senv = launch_record(log)
        if c.check("#583 a role job launches claude",
                   not (r.returncode or argv is None),
                   f"exit {r.returncode}: {(r.stderr or r.stdout).strip()[:300]}"):
            c.check("#583 the claude of a role carries the bundle's prompt",
                    not (f"--append-system-prompt-file {env['PU_SOUL_DIR']}/bundle/soul.md"
                         not in argv), argv)
            prompt_path = argv.split("--append-system-prompt-file ", 1)[1].split()[0]
            with open(prompt_path, "rb") as f:
                c.check("#583 the appended prompt is the role's own bytes",
                        not (f.read() != role_triple("puppet")[1]), prompt_path)
            settings = f"{home}/.config/mop/claude/pu-mop-1/settings.json"
            mcp = f"{home}/.config/mop/claude/pu-mop-1/mcp.json"
            c.check("#583 the role session uses per-puppet --settings",
                    not (f"--settings {settings}" not in argv), argv)
            c.check("#583 the role session uses per-puppet --mcp-config",
                    not (f"--mcp-config {mcp}" not in argv), argv)
            c.check("#583 the role session registers no user-scope mcp",
                    not ("mcp add --scope user mop" in open(log).read()),
                    "claude mcp add ran")
            with open(settings) as f:
                doc = json.load(f)
            c.check("#583 the per-puppet settings carry hooks and statusline",
                    not (set(doc.get("hooks", {})) != HOOK_EVENTS
                         or "statusLine" not in doc), sorted(doc))
            with open(mcp) as f:
                doc = json.load(f)
            c.check("#583 the per-puppet mcp config names the pool's server",
                    not (doc.get("mcpServers", {}).get("mop", {}).get("args")
                         != ["mcp"]), doc)
            c.check("#583 the role session rides the principal's credential",
                    not (senv.get("MOP_BUS_CONFIG")
                         != f"{env['PU_SOUL_DIR']}/bus.json"), senv)
            c.check("#583 the role session sees its soul, name and node",
                    not (senv.get("MOP_SOUL_DIR") != f"{env['PU_SOUL_DIR']}/bundle"
                         or senv.get("MOP_PUPPET") != "pu-mop-1"
                         or senv.get("MOP_NODE") != "node-a"), senv)
    finally:
        shutil.rmtree(home)

    # (3) две роли на одном доме: настройки изолированы
    first = second = None
    for name, which in (("pu-mop-1", "puppet"), ("pu-mop-2", "builder")):
        env, home, log = fake_body(name, which)
        try:
            r = run_wrapper(home, env)
            argv, _ = launch_record(log)
            if which == "puppet":
                first = (r, argv)
            else:
                second = (r, argv)
        finally:
            shutil.rmtree(home)
    if c.check("#583 two roles on one home both launch",
               not (first is None or second is None
                    or first[0].returncode or second[0].returncode)):
        a, b = first[1], second[1]
        c.check("#583 each role gets its own settings file",
                not (a == b or "/claude/pu-mop-1/" not in a
                     or "/claude/pu-mop-2/" not in b), f"{a} vs {b}")
        c.check("#583 each role gets its own prompt bytes",
                not (a.split("--append-system-prompt-file ", 1)[1].split()[0]
                     == b.split("--append-system-prompt-file ", 1)[1].split()[0]),
                "the same prompt file for two roles")

    # (4) промпт роли живёт и на продолжении истории: маркер в клоне
    # старый (токен сменился -- перерегистрация), --continue срабатывает
    env, home, log = fake_body("pu-mop-1", "puppet", cont=True,
                               env_extra={"PU_CONTINUE": "tok-583"})
    try:
        marker = os.path.join(env["PU_CLONE"], ".git", "mop-continue")
        with open(marker, "w") as f:
            f.write("an older token")
        r = run_wrapper(home, env)
        argv, _ = launch_record(log)
        if c.check("#583 a resumed role session launches",
                   not (r.returncode or argv is None),
                   f"exit {r.returncode}: {(r.stderr or r.stdout).strip()[:300]}"):
            c.check("#583 the resume carries both --continue and the prompt",
                    not ("--continue" not in argv
                         or "--append-system-prompt-file" not in argv), argv)
    finally:
        shutil.rmtree(home)

    # (5) три роли -- один старт: разница только в данных (пути нормализуем:
    # у каждого прогона свой временный дом, имя файла промпта -- из
    # манифеста своей роли: soul.md у рабочего, SKILL.md у мастера)
    starts = []
    for which in ("puppet", "builder", "master"):
        env, home, log = fake_body("pu-mop-1", which)
        try:
            r = run_wrapper(home, env)
            argv, _ = launch_record(log)
            if argv is not None:
                starts.append(re.sub(re.escape(home), "$HOME",
                                     re.sub(re.escape(env["PU_SOUL_DIR"]),
                                            "$SOUL", argv))
                              .replace("/soul.md", "/PROMPT")
                              .replace("/SKILL.md", "/PROMPT"))
        finally:
            shutil.rmtree(home)
    c.check("#583 three roles start identically, differences are data",
            not (len(starts) != 3 or len(set(starts)) != 1), starts)

    # (6) расхождение -- отказ до модели, без легаси-отката
    bad_ready = {"ready": True, "role": "puppet", "soul_inc": INC,
                 "soul_digest": "ff" * 32}
    tampered = {  # бандл с подменённым промптом: digest не сойдётся с метой
        "soul.yaml": json.dumps(role_triple("puppet")[0], sort_keys=True) + "\n",
        "soul.md": "rewrite me",
    }
    no_prompt = {"soul.yaml": json.dumps(role_triple("puppet")[0],
                                         sort_keys=True) + "\n"}
    extra_file = dict(no_prompt)
    extra_file["soul.md"] = role_triple("puppet")[1].decode()
    extra_file["stowaway.txt"] = "x"
    m, p, d = role_triple("puppet")
    cases = [
        ("no ready receipt", lambda h, n: plant_soul(h, n, m, p, d, ready=False)),
        ("the receipt names another digest", lambda h, n: plant_soul(
            h, n, m, p, d, ready=bad_ready)),
        ("the bundle hash does not pin the meta", lambda h, n: plant_soul(
            h, n, m, p, d, files=tampered)),
        ("a partial bundle", lambda h, n: plant_soul(h, n, m, p, d, files=no_prompt)),
        ("a stowaway file in the bundle", lambda h, n: plant_soul(
            h, n, m, p, d, files=extra_file)),
        ("no principal credential", lambda h, n: plant_soul(h, n, m, p, d,
                                                            creds=False)),
        ("the credential names no grant principal", lambda h, n: plant_soul(
            h, n, m, p, d, creds={"url": "nats://10.0.0.1:4222",
                                  "user": "puppet-mop", "password": "p"})),
    ]
    for what, plant in cases:
        env, home, log = fake_body("pu-mop-1", "puppet", plant=plant)
        try:
            r = run_wrapper(home, env)
            argv, _ = launch_record(log)
            c.check(f"#583 {what}: refusal before the model, with a reason",
                    not (r.returncode == 0 or argv is not None
                         or not r.stderr.strip()),
                    f"exit {r.returncode}, launched {argv is not None}")
        finally:
            shutil.rmtree(home)
    env, home, log = fake_body("pu-mop-1", "puppet",
                               env_extra={"PU_NODE": ""})
    try:
        r = run_wrapper(home, env)
        argv, _ = launch_record(log)
        c.check("#583 a role without a node identity refuses, no hostname guess",
                not (r.returncode == 0 or argv is not None),
                f"exit {r.returncode}, launched {argv is not None}")
    finally:
        shutil.rmtree(home)

    # (7) кавычки и флаг: вратам нечего экранировать, флаг знает CLI
    c.check("#583 the wrapper still travels base64: no $$ in it",
            not ("$$" in spec.WRAPPER))
    for inputs in SPEC_INPUTS:
        job = spec.job_spec(*inputs[:2], cont=inputs[2])
        plain = json.dumps(job)
        c.check(f"{inputs[0]}: no substitution outside the base64 wrapper (#583)",
                not (not set(re.findall(r"\$\{[^}]*\}", plain))
                     <= {spec.PROJECTS_TARGET, spec.MEM_CAP_TARGET}),
                sorted(set(re.findall(r"\$\{[^}]*\}", plain))))
    claude = shutil.which("claude", path=os.environ["PATH"])
    if claude is not None:
        r = subprocess.run([claude, "--help"], capture_output=True, text=True,
                           timeout=60)
        c.check("#583 the installed claude knows --append-system-prompt[-file]",
                not (re.search(r"--append-system-prompt(\[-file\])?",
                               r.stdout + r.stderr) is None),
                "the flag is absent from --help")


# ── #583 (ревью 2587d78): закрытые замечания разбора ──────────────────────
# HYPOTHESIS: ревью нашло шесть дыр в принятой версии и порядок старта.
# (а) ветвление только по непустому PU_SOUL: джоб с DIGEST/INC/DIR без
#     имени роли -- сломанная запись, а обёртка молча вела бы её легаси-путём;
# (б) врата сверяли digest, но не имя роли в бандле с ожидаемым: чужой
#     бандл с правильным digest и чужой меткой поднялся бы под именем роли;
# (в) role_prompt/role_settings клеились в строку команды без кавычек:
#     имя файла промпта с пробелом ломало argv, метасимволы -- синтаксис;
#     грамматика имени файла в soul.py не запрещает ни того, ни другого;
# (г) MOP_BUS_CONFIG ролевого ехал конкатенацией с проектным и перетирался
#     вторым -e -- кред выбирался дважды и ни разу явно;
# (д) легаси-хуки: `if ... && ... else echo` печатал «could not write»
#     на каждом успешном ролевом старте -- ложный сбой в логе задачи;
# (е) грамматика принципала дублировалась в heredoc вместо soul.is_grant;
# (ж) #590 готовит клон ДО врапера (bootstrap, identity хозяина, метка), а
#     CLONE_SH в врапере на чужом origin сносит клон и клонирует заново --
#     снося поставленную identity и маркеры; после такого второго прогона
#     подготовка роли исчезла бы до первого хода модели.
# SOLUTION: (а) ролевая ветка выбирается по ЛЮБОМУ непустому PU_SOUL*, а
# не по имени роли; неполный набор в теле -- отказ (легаси-конвенция -- все
# четыре пустые); (б) врата сверяют и имя роли бандла; (в) argv собирается
# массивом, пути -- отдельные элементы без склейки; (г) кред выбирается
# один раз до запуска; (д) jq-блок хуков один, путь -- параметр; (е) врата
# зовут soul.is_grant; (ж) ролевой старт валидирует ГОТОВЫЙ клон (origin
# совпадает, .git на месте) и на расхождении отказывает -- пересоздавать
# клон после инициализации обёртка не имеет права, легаси-путь не тронут.
# RESULT: все семь пунктов закрыты на живом прогоне врапера в поддельных
# телах; легаси-путь сохранил прежнее поведение (общий файл хуков,
# регистрация mcp, кред проекта), слепок перегенерирован.
# STATUS: FIXED — see #583
def check_role_review_583(c):
    import shutil
    import subprocess
    import tempfile
    from mop import driver
    from mop.common import soul

    manifest, prompt, digest = role_triple("puppet")

    # (а) частичная ролевая мета -- отказ в ЛЮБОМ направлении: пустое имя
    # роли при непустых спутниках и наоборот. Транспорт #582 держит все
    # четыре ключа на обоих путях (легаси -- пустыми), поэтому раскладка
    # «трое есть, имени нет» -- не легаси и не роль, а поломка.
    for what, over in (("PU_SOUL is empty but the triple rides",
                        {"PU_SOUL": ""}),
                       ("PU_SOUL names a role, the digest is empty",
                        {"PU_SOUL": "puppet", "PU_SOUL_DIGEST": ""})):
        # Первый случай гасит только имя (остальное честно от доставки),
        # второй -- только digest: каждое направление неполноты отдельно.
        env, home, log = fake_body("pu-mop-1", "puppet")
        env.update(over)
        try:
            r = run_wrapper(home, env)
            argv, _ = launch_record(log)
            c.check(f"#583 review {what}: refusal, never a silent legacy boot",
                    not (r.returncode == 0 or argv is not None
                         or not r.stderr.strip()),
                    f"exit {r.returncode}, launched {argv is not None}")
        finally:
            shutil.rmtree(home)
    # и легаси-конвенция «все четыре пустые» остаётся легаси
    env, home, log = fake_body("pu-mop-1", env_extra={
        "PU_SOUL": "", "PU_SOUL_DIGEST": "", "PU_SOUL_INC": "",
        "PU_SOUL_DIR": ""})
    try:
        r = run_wrapper(home, env)
        argv, _ = launch_record(log)
        c.check("#583 review all-empty PU_SOUL keys stay the legacy path",
                not (r.returncode or argv is None),
                f"exit {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
    finally:
        shutil.rmtree(home)

    # (б) чужое имя роли в бандле при верном digest: receipt и бандл лгут
    # СОГЛАСОВАННО -- расписка называет роль джоба, digest совпадает с
    # метой, а сам бандл собран с чужой меткой soul. Digest этого не ловит
    # (он от бандла), ловит только сверка ИМЕНИ роли бандла с ожиданием.
    liar = dict(manifest)
    liar["soul"] = "impostor"
    liar_bundle = soul.from_manifest(liar, prompt)
    env, home, log = fake_body("pu-mop-1", plant=lambda h, n: None)
    try:
        root = plant_soul(home, "pu-mop-1", liar, prompt, liar_bundle.digest,
                          ready={"ready": True, "role": "puppet",
                                 "soul_inc": INC,
                                 "soul_digest": liar_bundle.digest})
        env.update({"PU_SOUL": "puppet", "PU_SOUL_DIGEST": liar_bundle.digest,
                    "PU_SOUL_INC": INC, "PU_SOUL_DIR": root,
                    "PU_NODE": "node-a"})
        r = run_wrapper(home, env)
        argv, _ = launch_record(log)
        c.check("#583 review a bundle of another role name with a matching "
                "digest refuses",
                not (r.returncode == 0 or argv is not None
                     or not r.stderr.strip()),
                f"exit {r.returncode}, launched {argv is not None}")
    finally:
        shutil.rmtree(home)
    # ...а честный бандл своей роли -- стартует
    env, home, log = fake_body("pu-mop-1", "puppet")
    try:
        r = run_wrapper(home, env)
        argv, _ = launch_record(log)
        c.check("#583 review the matching bundle still launches",
                not (r.returncode or argv is None),
                f"exit {r.returncode}")
    finally:
        shutil.rmtree(home)

    # (в) имя файла промпта с пробелом и метасимволами: грамматика
    # soul._filename их не запрещает, доставка кладёт файл как есть -- argv
    # обязан нести путь одним элементом. Роль строим from_manifest с
    # именем промпта из пробелов/метасимволов и своим digest'ом ожидания.
    weird = "a b;$(id).md"
    wm, wp = dict(manifest), prompt
    wm["prompt"] = weird
    weird_bundle = soul.from_manifest(wm, wp)
    env, home, log = fake_body("pu-mop-1", plant=lambda h, n: None)
    try:
        root = plant_soul(home, "pu-mop-1", wm, wp, weird_bundle.digest,
                          ready={"ready": True, "role": "puppet",
                                 "soul_inc": INC,
                                 "soul_digest": weird_bundle.digest})
        # ожидание джоба -- по digest'ом странного бандла
        env.update({"PU_SOUL": "puppet", "PU_SOUL_DIGEST": weird_bundle.digest,
                    "PU_SOUL_INC": INC, "PU_NODE": "node-a",
                    "PU_SOUL_DIR": root})
        r = run_wrapper(home, env)
        argv, _ = launch_record(log)
        if c.check("#583 review a space- and metachar-bearing prompt name "
                   "launches as one argv element",
                   not (r.returncode or argv is None),
                   f"exit {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}"):
            body = argv.split(" ", 1)[1] if argv else ""
            c.check("#583 review the weird prompt path is not shell-split",
                    not (f"{root}/bundle/{weird}" not in body
                         or "$(id)" in body and "$(" in body.split(
                             "--append-system-prompt-file ")[-1].replace(
                             f"$(id)", "", 1) is None),
                    argv)
            c.check("#583 review the id command never ran",
                    not ("uid=" in (r.stdout + r.stderr)),
                    r.stdout[:200])
    finally:
        shutil.rmtree(home)

    # (г) кред выбирается один раз: в сессии -- только кред роли, и в
    # тексте обёртки нет второй записи MOP_BUS_CONFIG (проверка текста --
    # в check_role_startup_583 не лазит, здесь -- по журналу запуска)
    env, home, log = fake_body("pu-mop-1", "puppet")
    try:
        r = run_wrapper(home, env)
        _, senv = launch_record(log)
        c.check("#583 review the role session carries the principal credential "
                "once, never the project's",
                not (senv.get("MOP_BUS_CONFIG")
                     != f"{env['PU_SOUL_DIR']}/bus.json"),
                senv.get("MOP_BUS_CONFIG"))
        w = spec.WRAPPER
        c.check("#583 review the wrapper names MOP_BUS_CONFIG exactly once",
                not (w.count("MOP_BUS_CONFIG=") != 1),
                w.count("MOP_BUS_CONFIG="))
    finally:
        shutil.rmtree(home)

    # (д) успешный ролевой старт не пишет «could not write» в лог задачи
    env, home, log = fake_body("pu-mop-1", "puppet")
    try:
        r = run_wrapper(home, env)
        argv, _ = launch_record(log)
        if c.check("#583 review a successful role boot still launches",
                   not (r.returncode or argv is None), r.returncode):
            c.check("#583 review a successful role boot logs no fake hooks "
                    "failure",
                    not ("could not write" in r.stderr),
                    r.stderr[:200])
            c.check("#583 review the hooks jq block appears once, path is a "
                    "variable",
                    not (spec.WRAPPER.count("--arg cmd") != 1),
                    spec.WRAPPER.count("--arg cmd"))
    finally:
        shutil.rmtree(home)

    # (е) грамматика принципала не дублируется: врата зовут soul.is_grant,
    # своей проверки grant-<hex> в тексте нет
    c.check("#583 review the grant grammar is soul.is_grant, not a wrapper "
            "copy",
            not ("grant-" in role_section() and "is_grant" not in role_section()),
            "the heredoc re-implements the grant grammar")

    # (ж) клон после подготовки #590: чужой origin в ГОТОВОМ клоне --
    # отказ, не пересоздание. Identity хозяина и маркер обязаны пережить
    # ролевой старт обёртки до первого хода модели.
    env, home, log = fake_body("pu-mop-1", "puppet")
    try:
        clone = env["PU_CLONE"]
        marker = os.path.join(clone, ".git", "mop-continue")
        with open(marker, "w") as f:
            f.write("identity marker")
        subprocess.run(["git", "-C", clone, "remote", "set-url", "origin",
                        "git@git.example.dev:someone/OTHER.git"], check=True)
        r = run_wrapper(home, env)
        argv, _ = launch_record(log)
        c.check("#583 review a role refuses on a prepared clone with a wrong "
                "origin, no re-clone",
                not (r.returncode == 0 or argv is not None),
                f"exit {r.returncode}, launched {argv is not None}")
        c.check("#583 review the identity marker survives the refusal",
                os.path.exists(marker) and open(marker).read() == "identity marker",
                "the marker is gone -- the wrapper re-prepared the clone")
    finally:
        shutil.rmtree(home)
    # ...а совпадающий origin клон не трогает и стартует
    env, home, log = fake_body("pu-mop-1", "puppet")
    try:
        clone = env["PU_CLONE"]
        marker = os.path.join(clone, ".git", "mop-continue")
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w") as f:
            f.write("identity marker")
        r = run_wrapper(home, env)
        argv, _ = launch_record(log)
        if c.check("#583 review a role launches on a matching prepared clone",
                   not (r.returncode or argv is None),
                   f"exit {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}"):
            c.check("#583 review the prepared clone keeps its marker",
                    os.path.exists(marker), "the marker is gone")
    finally:
        shutil.rmtree(home)


def main():
    if sys.argv[1:] == ["--snapshot"]:
        # Снять слепок заново: только осознанно, когда спека меняется нарочно
        # и все папеты всё равно перерегистрируются. Ролевой случай (#583) --
        # в том же слепке, своим ключом: спека роли отличается от легаси
        # не только текстом врапера.
        cases = {**{" ".join(str(x) for x in i): render(*i) for i in SPEC_INPUTS},
                 " ".join(str(x) for x in ROLE_RENDER) + " role":
                     render_role(*ROLE_RENDER)}
        with open(SNAPSHOT, "w") as f:
            json.dump(cases, f, ensure_ascii=False, indent=1)
            f.write("\n")
        return 0
    c = Checks()
    # #714: старый импорт ещё допустим историческим callers до #645;
    # modern renderer и его шаблон принадлежат только launch_spec.
    from mop.server import launch_spec
    for name in ('api_job_spec', 'API_WRAPPER', 'API_LAUNCH_CAPABILITIES'):
        c.check(f'#714 historical spec reexports {name}',
                getattr(spec, name) is getattr(launch_spec, name))
    for fn in (check_snapshot, check_template_version, check_llm_env_449,
               check_ca_section_557,
               check_wrapper_paths,
               check_git_identity, check_driver_free_183,
               check_memory_197, check_project_asks_197, check_spec_memory_197,
               check_nomad_order_197, check_claude_hooks_223, check_hook_events_341,
               check_no_feedback_420, check_statusline_455, check_job_meta_265,
               check_node_pin_289, check_role_startup_583, check_role_review_583):
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
