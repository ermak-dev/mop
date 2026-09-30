"""Общая часть командлетов.

Живёт в mop/cli, а не глубже в пакете, намеренно: здесь печатают. Библиотека
`mop` возвращает данные и молчит — иначе MCP-сервер начал бы разбирать текст,
свёрстанный для терминала.

Командлет пользуется этим так:

    from mop.cli import lib
    from mop.common import puppets

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

from mop.common import bus, config, creds, puppets, render  # noqa: E402
from mop.common.domain import JobMeta  # noqa: E402


# Каталоги установки: корень проекта и bin/ с единственным исполняемым
# файлом (`mop master` вписывает его в конфиг MCP для claude).
PROJECT = config.PROJECT
BIN = os.path.join(PROJECT, "bin")


def self_argv(bin_dir=None, python=None):
    """Чем mop зовёт сам себя -> начало argv (#368, правило #351). У клона --
    его bin/mop: команда идёт на дереве той же копии. У пакета bin/ нет, и
    его зовёт тот же интерпретатор, что запустил mop, -- в venv пакета.
    Умолчания читаются при вызове, а не при определении: BIN подменяют."""
    launcher = os.path.join(BIN if bin_dir is None else bin_dir, "mop")
    if os.path.exists(launcher):
        return [launcher]
    return [sys.executable if python is None else python, "-m", "mop.cli"]


# Цвета и отказ строкой -- в mop.cli.term (#320): их берёт и сборка
# страницы, которой lib с шиной не поднять (#302).
from mop.cli.term import fail, ok, section, usage  # noqa: E402,F401


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
        # `mop server deploy`.
        config.require("MOP_SERVER_LAN")
        return fn(argv)
    return wrap


def parse_value(args, flag, once=False):
    """Выкусить `flag VALUE` (или `flag=VALUE`) откуда угодно в аргументах
    -> (значение | None, остальные). Повтор -- последнее значение, с once --
    отказ. RuntimeError: диспетчер делает из него одну строку в stderr (#146)."""
    value, rest, it = None, [], iter(args)
    for a in it:
        if a == flag or a.startswith(flag + "="):
            if once and value is not None:
                raise RuntimeError(f"{flag} given twice")
            if a == flag:
                value = next(it, "")
                # Следом флаг, а не значение: `--llm --fresh` съедал бы
                # соседний флаг как значение (#164).
                if value.startswith("-"):
                    rest.append(value)
                    value = ""
            else:
                value = a.split("=", 1)[1]
        else:
            rest.append(a)
    if value == "":
        # Забытое значение -- ошибка использования, а не «флага нет»: иначе
        # `--cred` без имени молча заводил папета на первой аренде (#327).
        raise RuntimeError(f"{flag} needs a value")
    return value, rest


def parse_named(argv, doc, most=1):
    """`<имя> [ещё...] [--force]` -> ([имя, ещё...], force). Чистая функция.
    --force где угодно; имён от одного до most, иначе usage(doc)."""
    force = "--force" in argv
    args = [a for a in argv if a != "--force"]
    if not 1 <= len(args) <= most:
        usage(doc)
    return args, force


def named(argv, doc):
    """Команда над одним папетом (delete, recycle, restart, wipe, #263):
    разбор `<имя> [--force]` и перила guard. -> (имя, force)."""
    args, force = parse_named(argv, doc)
    guard(args[0])
    return args[0], force


def note(name, reply):
    """Чью аренду прошёл --force (#40): строкой «имя: ...», если ответ её несёт."""
    if reply.get("owner_note"):
        print(f"{name}: {reply['owner_note']}")


def dry(argv, doc):
    """`[--dry]` -> bool; иное -- usage(doc) (gc, sweep, driver sweep)."""
    if argv and argv != ["--dry"]:
        usage(doc)
    return argv == ["--dry"]


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
    """Интеграционная ветка как `origin/<имя>`: ветка человека из контекста
    (#249: git config mop.branch, MOP_BRANCH), иначе ветка по умолчанию у
    origin. origin/HEAD выставлен не в каждом клоне — откат на master, потому
    что отказ здесь означал бы «не могу завести ветку» там, где ветку
    завести можно."""
    from mop.common import context
    mine = context.current().branch
    if mine:
        return f"origin/{mine}"
    r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "origin/HEAD"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "origin/master"


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
    owner = JobMeta.from_meta(spec.get("meta")).project
    if owner != project:
        sys.exit(f"{name} — project {owner}, but this master runs {project}. "
                 f"Leave the master shell or run mop master for {owner}.")
    return spec


def stderr_text(name, why, lines):
    """Хвост stderr аллокации вместо пейна (#333) -> строки вывода: сначала
    -- что это stderr и почему не пейн (первая строка причины), затем сам
    хвост. Один текст на `mop tail` и инструмент MCP `tail`."""
    reason = (str(why).splitlines() or [""])[0]
    head = f"{name}: no tmux session ({reason}); the allocation's stderr"
    if not lines:
        return [f"{head} is empty"]
    return [f"{head}, last {len(lines)} lines:", *lines]


def pool_lines():
    try:
        out = []
        for n in puppets.pool():
            if n.status != "ready":
                out.append(f"  {n.name}: {n.status}")
            elif n.error:
                out.append(f"  {n.name}: {n.error}")
            else:
                # Вниз, как `mop node` и страница (#329): свободного не бывает
                # больше, чем есть, -- округлённые 1536 МБ читались как «2 GB».
                out.append(f"  {n.name}: free {n.free_mb // 1024}/"
                           f"{n.total_mb // 1024} GB, "
                           f"slots {render.ratio(n.slots, n.slots_total)}")
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
