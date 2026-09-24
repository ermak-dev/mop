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
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys

from mop import bus, config, creds, identity, keys, llm, playvars, puppets  # noqa: E402


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


# Управляющие последовательности и символы строки чужого вывода: в кадре
# они красили бы его и двигали курсор, а ширина считается по видимому.
_ESCAPES = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]|[\x00-\x08\x0b-\x1f\x7f]")


def plain(text):
    """Текст без цветов и управляющих символов терминала: вывод командлета,
    который читает модель, а не терминал (#160)."""
    return _ESCAPES.sub("", text)


def frame(drawn, rows, width):
    """Кадр хода на терминале (#140): что записать, чтобы drawn строк,
    нарисованных прежде, сменились строками rows. Курсор стоит в конце
    последней строки кадра. Строки -- без управляющих символов и обрезаны
    по ширине: перенос строки сбил бы счёт высоты, и кадр полз бы вниз."""
    up = f"\033[{drawn - 1}A" if drawn > 1 else ""
    clean = [_ESCAPES.sub("", r.replace("\t", " "))[:width - 1] for r in rows]
    return "\r" + up + "\033[J" + "\n".join(clean)


class Progress:
    """Ход долгой команды: строка шага, перерисовываемая на месте (#124).

    Быстрая команда при успехе молчит, долгая показывает, что делает сейчас,
    и больше ничего: ни эха параметров, ни справок, ни советов. Строка живёт
    только на терминале и стирается по завершении; не на терминале (скрипт,
    модель) -- тишина до ошибки.

    Вывод, который идёт долго (ansible сборки, #140), -- log(): на терминале
    над строкой шага окно последних WINDOW строк, стирается вместе с ней.
    verbose -- каждая строка в stdout насовсем, и не на терминале тоже."""

    WINDOW = 8

    def __init__(self, what, verbose=False):
        import collections
        import time
        self._time = time.time
        self.what, self.t0 = what, time.time()
        self.tty = sys.stderr.isatty()
        self.verbose = verbose
        self.window = collections.deque(maxlen=self.WINDOW)
        self.text, self.drawn = None, 0

    def step(self, text):
        self.text = text
        self._draw()

    def log(self, lines):
        if self.verbose:
            self._erase()
            for line in lines:
                print(line, flush=True)
        else:
            self.window.extend(lines)
        self._draw()

    def clear(self):
        self._erase()
        self.window.clear()

    def _draw(self):
        if not self.tty or self.text is None:
            return
        s = int(self._time() - self.t0)
        rows = [*self.window, f"{self.what}: {self.text} [{s // 60}:{s % 60:02d}]"]
        width = shutil.get_terminal_size((80, 20)).columns
        sys.stderr.write(frame(self.drawn, rows, width))
        sys.stderr.flush()
        self.drawn = len(rows)

    def _erase(self):
        if self.drawn:
            width = shutil.get_terminal_size((80, 20)).columns
            sys.stderr.write(frame(self.drawn, [], width))
            sys.stderr.flush()
            self.drawn = 0


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


def cwd_origin():
    """origin текущей рабочей копии, либо None: не рабочая копия или у неё
    нет origin. Отказывать -- дело вызывающего: только он знает, как себя
    назвать в отказе (#111)."""
    try:
        return subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True).stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def workspace_text(origin):
    """workspace папета (#133): .mop/bootstrap.yaml рабочей копии проекта,
    если команда идёт из неё, иначе из origin (ветка по умолчанию). Нет
    файла -- пустой текст: сервер снимает копию папета, и удаление доходит."""
    from mop import bootstrap, manifest
    if cwd_origin() == origin:
        top = git("rev-parse", "--show-toplevel")
        try:
            with open(os.path.join(top, bootstrap.FILE)) as f:
                return f.read()
        except FileNotFoundError:
            return ""
    return manifest.fetch(origin)["bootstrap_text"] or ""


def pick_origin(arg, here):
    """Какой origin взять команде. Чистая функция (#111).

    arg -- явный аргумент или None, here -- origin рабочей копии или None.
    Явный старше рабочей копии. Неверный явный -- отказ, а не подмена
    рабочей копией: опечатка иначе молча завела бы не тот проект. Отказ --
    ValueError с причиной: None дальше читался бы как проект с пустым
    именем."""
    if arg is not None:
        if not puppets.looks_like_origin(arg):
            raise ValueError(f"{arg!r} doesn't look like a git-origin "
                             f"(a host or a path is expected, not a project name)")
        return arg.strip()
    if here:
        return here
    raise ValueError("not in a git working copy with an origin, and no origin given")


def origin(arg, doc):
    """Origin для команды: явный аргумент, иначе рабочая копия (#111).

    Единый вход для всех команд, которым нужен origin проекта. doc -- то,
    что показать после причины отказа (обычно докстринг команды): отказ
    называет ту команду, что спрашивала, а не `mop add`."""
    try:
        return pick_origin(arg, None if arg is not None else cwd_origin())
    except ValueError as e:
        usage(f"{e}\n\n{doc.strip()}")


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


def play_vars(projects, manifests=None, limits=None, git_hosts=None, inventory_hosts=None):
    """Списки плейбуку как --extra-vars, JSON'ом. -> [строки].

    Объектом, а не парой ключ=значение: `--extra-vars mop_projects=[...]`
    ansible принимает как строку, и цикл в шаблоне честно проходится по её
    символам, порождая пользователей `master-[`, `master-"` и так далее.

    Манифесты — только у полной игры: узкому прогону проектов (bus) они не
    нужны, их читают слои узла и тела."""
    # Лимиты папетов (#107) едут рядом с проектами, и пустые тоже: сервис
    # кластера на сервере читает их файлом, и узкий прогон `mop project`
    # обязан класть его так же, как полный. Файл читает play(), не эта
    # функция: она чистая.
    head = {"mop_projects": list(projects)}
    if limits is not None:
        head["mop_limits"] = limits
    # Хосты форжей (#121) -- только полной игре: их читает роль узла.
    if git_hosts is not None:
        head["mop_git_hosts"] = list(git_hosts)
    # Хосты инвентаря (#178) -- только полной игре: роль cluster кладёт их
    # файлом, и по нему forget отказывает узлу, который deploy поставит снова.
    if inventory_hosts is not None:
        head["mop_inventory_hosts"] = list(inventory_hosts)
    out = [json.dumps(head)]
    if manifests is not None:
        out.append(json.dumps({"mop_manifests": manifests}, ensure_ascii=False))
    return out


def play_env(get):
    """Окружение процесса ansible сверх своего. -> {имя: значение}.

    Пароль служебной учётки LDAP (#214) -- только когда ldap в цепочке (#232), и
    окружением, а не --extra-vars: argv виден в списке процессов, а
    настройки едут плейбукам все. Плейбук берёт его lookup('env') и кладёт
    файлом 0600 в /etc/nats/identity."""
    if "ldap" not in identity.links(get("MOP_AUTH_PROVIDER")):
        return {}
    return {"MOP_LDAP_BIND_PASSWORD": get("MOP_LDAP_BIND_PASSWORD") or ""}


def play(playbook, projects, manifests=None, git_hosts=None, check=False,
         inventory_hosts=None):
    """Прогон плейбука установки. -> код возврата ansible.

    Один вход для полной игры (site.yml) и для узкого прогона проектов
    (до #117): списки, которые едут плейбуку, собираются одним
    местом, иначе узкий прогон заводил бы проект не так, как полный.

    Списки едут --extra-vars ОБЪЕКТОМ, а не парой ключ=значение:
    `--extra-vars mop_projects=[...]` ansible принимает как строку, и цикл в
    шаблоне честно проходится по её символам, порождая пользователей
    `master-[`, `master-"` и так далее.

    check=True -- прогон без изменений (#177): ansible --check --diff, и
    вывод прогона -- это разница, которую внёс бы настоящий.
    """
    if not shutil.which("ansible-playbook"):
        raise RuntimeError("no ansible-playbook on this machine -- run mop setup")
    inventory = os.environ["INVENTORY"]
    if not os.path.isfile(inventory):
        raise RuntimeError(f"no inventory {inventory} -- create it from the example: "
                           f"cp inventory.yaml.example inventory.yaml")
    vars_ = playvars.playbook_vars()
    # Лимиты (#107) больше не едут: их держит и правит сервер (#117).
    extra = ([json.dumps(vars_, ensure_ascii=False)]
             + play_vars(projects, manifests, git_hosts=git_hosts,
                         inventory_hosts=inventory_hosts))
    return subprocess.call(
        ["ansible-playbook", "-i", inventory, os.path.join(PROJECT, playbook),
         *sum((["--extra-vars", v] for v in extra), []),
         *(["--check", "--diff"] if check else [])],
        env={**os.environ, **play_env(config.get)})


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
    """Перила мастер-шелла: не трогать чужого папета. -> ответ `spec` либо
    None у оператора вне `mop master`, которого перила не касаются.

    Это не граница — MOP_PROJECT оператор может и снять. Настоящая живёт в кредах
    NATS, в проверке агента и в проверке сервиса кластера (#80). Здесь мы лишь
    не даём промахнуться вслепую: отказ отсюда называет, куда идти, а отказ
    сервиса — чей это папет. Прочитанную спеку отдаём: иначе вызывающий
    спрашивал бы её вторым таким же запросом (#146)."""
    project = in_project()
    if project is None:
        return None
    spec = bus.call_cluster("spec", name=name)
    meta = spec.get("meta") or {}
    owner = puppets.project_of(meta.get("origin", ""))
    if owner != project:
        sys.exit(f"{name} — project {owner}, but this master runs {project}. "
                 f"Leave the master shell or run mop master for {owner}.")
    return spec


def parse_llm(args):
    """Выкусить --llm PROFILE (или --llm=PROFILE) откуда угодно в аргументах.
    -> (профиль | None, остальные аргументы)."""
    profile, rest, it = None, [], iter(args)
    for a in it:
        if a == "--llm":
            profile = next(it, "")
            # Следом флаг, а не имя: `--llm --fresh` съедал бы соседний флаг
            # как профиль и отказывал про профиль «--fresh».
            if profile.startswith("-"):
                rest.append(profile)
                profile = ""
        elif a.startswith("--llm="):
            profile = a.split("=", 1)[1]
        else:
            rest.append(a)
    if profile == "":
        # Забытое значение -- ошибка использования, а не «нет профиля ''»
        # (#164). RuntimeError: диспетчер делает из него одну строку в
        # stderr, как из любого отказа (#146).
        raise RuntimeError(f"--llm needs a profile name; available: "
                           f"{', '.join(llm.profiles())}")
    if profile is not None:
        llm.require(profile)
    return profile, rest


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
    """Ключи профиля на узлы. При успехе молчит (#124); не дошедшие --
    ошибкой, с узлами."""
    results = keys.push_llm_keys(llm)
    if results is None:
        return
    bad = [f"{n}: {r}" for n, r in sorted(results.items()) if r != "OK"]
    if bad:
        fail(f"{puppets.SECRETS_FILE} did not reach every node: " + "; ".join(bad))


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


def serve(service):
    """Подписчик сервера под systemd (mop-cluster, mop-bootstrap,
    mop-builder): строки журнала -- в stdout, оттуда их берёт journald."""
    try:
        asyncio.run(service(lambda line: print(line, flush=True)))
    except KeyboardInterrupt:
        pass
    return 0
