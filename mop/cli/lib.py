"""Общая часть командлетов.

Живёт в mop/cli, а не глубже в пакете, намеренно: здесь печатают. Библиотека
`mop` возвращает данные и молчит — иначе MCP-сервер начал бы разбирать текст,
свёрстанный для терминала.

Командлет пользуется этим так:

    from mop.cli import lib
    from mop import puppets

    def main(argv):
        ...
    main = lib.cluster(main)     # если команде нужен настроенный кластер

Диспетчер (mop/cli/__init__.py) зовёт `lib.run(main, argv)` сам.
"""
import json
import os
import shutil
import subprocess
import sys

from mop import bus, config, creds, keys, llm, puppets  # noqa: E402


# Каталоги установки: корень проекта и bin/ с единственным исполняемым
# файлом (`mop master` вписывает его в конфиг MCP для claude).
PROJECT = config.PROJECT
BIN = os.path.join(PROJECT, "bin")


# Цвета терминала — для `mop deploy`, у которого прогон длинный и заголовки
# разделов нужны глазу. Печатает только cli.
_RED, _GREEN, _BOLD, _NC = "\033[0;31m", "\033[0;32m", "\033[1m", "\033[0m"


def section(text):
    print(f"\n{_BOLD}{text}{_NC}", flush=True)


def fail(text):
    print(f"{_RED}{text}{_NC}", file=sys.stderr, flush=True)


def ok(text):
    print(f"{_GREEN}{text}{_NC}", flush=True)


def cluster(fn):
    """Командлет, которому нужен настроенный кластер. Проверка обязательных
    настроек — до первого сетевого вызова, чтобы отказ был про настройки, а не
    про таймаут к чужому адресу."""
    def wrap(argv):
        # Один адрес, а не весь REQUIRED: MOP_GIT_HOST читают плейбуки, и на
        # машине оператора его не с чего заполнять. Полный список спрашивает
        # `mop deploy`.
        config.require("MOP_SERVER_LAN")
        return fn(argv)
    return wrap


def usage(doc):
    sys.exit(doc.strip())


def cwd_origin(required=True):
    """origin текущей рабочей копии. required=False -> None вместо отказа."""
    try:
        return subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        if required:
            sys.exit("not a git working copy and no origin given: mop add <git-origin>")
        return None


def git(*args):
    """git в рабочей копии проекта. Отказ — строкой, а не трассировкой: она
    здесь ничего не добавляет, а скрывает единственное, что нужно знать."""
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def default_branch():
    """Ветка по умолчанию у origin. origin/HEAD выставлен не в каждом клоне —
    откат на master, потому что отказ здесь означал бы «не могу завести ветку»
    там, где ветку завести можно."""
    r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "origin/HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "origin/master"


# Код ansible «часть машин не ответила». Отличать его от настоящего отказа
# обязательно: выключенный узел — не сломанная команда, и сказать про него
# «проект, возможно, не на шине» значит отправить оператора искать поломку
# там, где её нет.
UNREACHABLE = 4


def play_vars(projects, manifests=None):
    """Списки плейбуку как --extra-vars, JSON'ом. -> [строки].

    Объектом, а не парой ключ=значение: `--extra-vars mop_projects=[...]`
    ansible принимает как строку, и цикл в шаблоне честно проходится по её
    символам, порождая пользователей `master-[`, `master-"` и так далее.

    Манифесты — только у полной игры: узкому прогону проектов (bus) они не
    нужны, их читают слои узла и тела."""
    out = [json.dumps({"mop_projects": list(projects)})]
    if manifests is not None:
        out.append(json.dumps({"mop_manifests": manifests}, ensure_ascii=False))
    return out


def play(playbook, projects, manifests=None):
    """Прогон плейбука установки. -> код возврата ansible.

    Один вход для полной игры (site.yml) и для узкого прогона проектов
    (deploy/projects.yml): списки, которые едут плейбуку, собираются одним
    местом, иначе узкий прогон заводил бы проект не так, как полный.

    Списки едут --extra-vars ОБЪЕКТОМ, а не парой ключ=значение:
    `--extra-vars mop_projects=[...]` ansible принимает как строку, и цикл в
    шаблоне честно проходится по её символам, порождая пользователей
    `master-[`, `master-"` и так далее.
    """
    if not shutil.which("ansible-playbook"):
        raise RuntimeError("no ansible-playbook on this machine -- run mop setup")
    inventory = os.environ["INVENTORY"]
    if not os.path.isfile(inventory):
        raise RuntimeError(f"no inventory {inventory} -- create it from the example: "
                           f"cp inventory.yaml.example inventory.yaml")
    vars_ = config.playbook_vars()
    # Без операторов на шину не войдёт ни один человек (#106): ролевых
    # admin и master-<проект> больше нет. Громкий отказ до прогона лучше,
    # чем установка, в которую никто не может войти.
    if not vars_["MOP_OPERATOR_SUBJECTS"]:
        raise RuntimeError("MOP_OPERATORS is empty: nobody could log in to the "
                           "bus. Name at least one person in .env, e.g. "
                           "MOP_OPERATORS=anton:admin")
    extra = ([json.dumps(vars_, ensure_ascii=False)]
             + play_vars(projects, manifests))
    return subprocess.call(
        ["ansible-playbook", "-i", inventory, os.path.join(PROJECT, playbook),
         *sum((["--extra-vars", v] for v in extra), [])])


def project_ready(project):
    """Есть ли у этой машины чем представиться шине за этот проект.

    Кред один -- человек (#84, #106): одни креды на все его проекты, и что
    именно ему положено, решает сервер NATS по роли, а не файл, -- поэтому
    здесь только «есть чем представиться», а отказ по правам приезжает с
    шины и читается отдельно (bus._silence). project остаётся в подписи:
    вопрос задают про проект, и ответ однажды может от него зависеть."""
    return creds.operator(creds.server_dir()) is not None


def in_project():
    """Проект этого шелла, либо None у оператора вне `mop master`."""
    return None if bus.PROJECT == bus.ADMIN else bus.PROJECT


def guard(name):
    """Перила мастер-шелла: не трогать чужого папета.

    Это не граница — MOP_PROJECT оператор может и снять. Настоящая живёт в кредах
    NATS, в проверке агента и в проверке сервиса кластера (#80). Здесь мы лишь
    не даём промахнуться вслепую: отказ отсюда называет, куда идти, а отказ
    сервиса — чей это папет."""
    project = in_project()
    if project is None:
        return
    meta = require_job(name).get("meta") or {}
    owner = puppets.project_of(meta.get("origin", ""))
    if owner != project:
        sys.exit(f"{name} — project {owner}, but this master runs {project}. "
                 f"Leave the master shell or run mop master for {owner}.")


def parse_llm(args):
    """Выкусить --llm PROFILE (или --llm=PROFILE) откуда угодно в аргументах.
    -> (профиль | None, остальные аргументы)."""
    profile, rest, it = None, [], iter(args)
    for a in it:
        if a == "--llm":
            profile = next(it, "")
        elif a.startswith("--llm="):
            profile = a.split("=", 1)[1]
        else:
            rest.append(a)
    if profile is not None:
        llm.require(profile)
    return profile, rest


def require_job(name):
    """Метаданные папета через шину: origin, профиль, статус, устарела ли
    спека. Целого джоба тут больше нет — его читал только spec_is_stale, и
    вердикт теперь приходит готовым от сервиса кластера (#81)."""
    got = bus.ask_cluster("spec", name=name)
    if got.get("error"):
        sys.exit(got["error"])
    return got


def alloc_of(name):
    """Аллокация папета и драйвер её узла, одним запросом. -> (alloc|None, драйвер)."""
    got = bus.ask_cluster("alloc", name=name)
    if got.get("error"):
        sys.exit(got["error"])
    return got.get("alloc"), got.get("driver")


def running_alloc(name):
    a, _ = alloc_of(name)
    if not a or a["ClientStatus"] != "running":
        sys.exit(f"{name} not running")
    return a


def running_node(name):
    """Узел папета — адрес для шины. Аллокация адресом быть перестала вместе
    с alloc exec; агент подписан на субъект узла."""
    return running_alloc(name)["NodeName"]


def session_env(profile):
    """Окружение сессии claude на профиле из mop/llm/: статическая часть
    профиля плюс ключ. -> (профиль, {переменные}).

    Источник ключа — местный .env, а не узловой secrets.env: на управляющей
    машине узел ничего не выдавал. Отказ, а не тишина: сессия без ключа
    отбивает каждый ход 401-й, а читается живой. Так поднимаются и мастер,
    и `mop code`."""
    prof = llm.require(profile)
    env = dict(prof["env"])
    if prof["key"]:
        key = config.get(prof["key"])
        if not key:
            usage(f"profile {profile}: no {prof['key']} in "
                  f"{puppets.LOCAL_KEYS_FILE} — add it and retry")
        env[prof["auth_var"]] = key
    return prof, env


def push_llm_keys(llm):
    """Ключи профиля на узлы, с отчётом. Молчит для профилей без ключа."""
    results = keys.push_llm_keys(llm)
    if results is None:
        return
    print(f"pushing secrets to pool nodes ({puppets.SECRETS_FILE})...")
    bad = [f"{n}: {r}" for n, r in sorted(results.items()) if r != "OK"]
    if bad:
        print("  not all nodes: " + "; ".join(bad))


def pool_lines():
    try:
        out = []
        for n in puppets.pool():
            if n["status"] != "ready":
                out.append(f"  {n['name']}: {n['status']}")
            elif "error" in n:
                out.append(f"  {n['name']}: {n['error']}")
            else:
                out.append(f"  {n['name']}: free {n['free_mb'] / 1024:.0f}/"
                           f"{n['total_mb'] / 1024:.0f} GB ({n['slots']} slots)")
        return out
    except Exception as e:
        return [f"  {e}"]
