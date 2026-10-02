"""Контекст команды: на какой сервер и под каким именем она идёт (#131).

Слои, каждый следующий перекрывает предыдущий по своему полю:

    рабочая копия   git config mop.server / mop.user / mop.branch -- привязка
                    клона (#125, #249); `--global` -- все клоны человека
    окружение       MOP_SERVER_LAN / MOP_BUS_USER / MOP_BRANCH
    командная строка  глобальный --server у любой команды, логин у join

Ветка (#249) -- интеграционная ветка человека, а не проекта: ветка по
умолчанию репозитория в GitLab одна на всех, а работать в свою `dev`,
оставив `master` веткой выкладки, нужно каждому по отдельности. Читает её
lib.default_branch (база ветки тикета у `mop dev bug start`), а сессия мастера
видит MOP_BRANCH и называет её папету целью landing.

Ни одного слоя -- поле пусто: клиент выбирает единственный joined-сервер,
при нескольких требует --server, серверные команды берут настройку установки.
Пароли в контекст не входят: они на машине человека в приватном каталоге
~/.config/mop/servers/<адрес>/, один вход на все проекты сервера.

Контекст ставит диспетчер вокруг командлета (`with use(...)`); библиотека
читает его через current(), config.get -- для MOP_SERVER_LAN. Внутри блока
он же в окружении: дочерние процессы (ansible, вложенный mop) видят тот же
сервер. На выходе -- прежний контекст и прежнее окружение.
"""
import contextlib
import contextvars
import os
import subprocess
from dataclasses import dataclass, field

from . import config

# Поле контекста -> (ключ git config, переменная окружения).
FIELDS = {"server": ("mop.server", "MOP_SERVER_LAN"),
          "user": ("mop.user", "MOP_BUS_USER"),
          "branch": ("mop.branch", "MOP_BRANCH")}


@dataclass(frozen=True)
class Context:
    server: str = None
    user: str = None
    branch: str = None     # интеграционная ветка человека (#249), без origin/
    # поле -> откуда взято: "clone", "env", "cli"
    sources: dict = field(default_factory=dict)


_current = contextvars.ContextVar("mop_context", default=Context())


# ─── чистое ──────────────────────────────────────────────────────────────
def resolve(cli, env, clone):
    """Слои -> Context. cli -- {server, user} из командной строки, env --
    окружение, clone -- {server, user} привязки клона."""
    values, sources = {}, {}
    for name, (_, var) in FIELDS.items():
        for source, value in (("clone", clone.get(name)), ("env", env.get(var)),
                              ("cli", cli.get(name))):
            if value:
                values[name], sources[name] = value, source
    return Context(server=values.get("server"), user=values.get("user"),
                   branch=values.get("branch"), sources=sources)


def strip_server(argv):
    """Глобальный --server с любого места argv. -> (сервер|None, остальное).
    Опция у всех команд одна, поэтому снимает её диспетчер, а не каждый
    командлет своим разбором."""
    server, rest, it = None, [], iter(argv)
    for a in it:
        if a == "--server":
            server = next(it, None)
            if not server:
                raise ValueError("--server needs an address")
        elif a.startswith("--server="):
            server = a.split("=", 1)[1]
            if not server:
                raise ValueError("--server needs an address")
        else:
            rest.append(a)
    return server, rest


# ─── процесс ─────────────────────────────────────────────────────────────
def clone_binding():
    """{server, user} привязки клона текущего каталога; не клон -- пусто."""
    out = {}
    for name, (key, _) in FIELDS.items():
        try:
            r = subprocess.run(["git", "config", "--get", key],
                               capture_output=True, text=True)
        except OSError:
            return {}
        if r.returncode == 0 and r.stdout.strip():
            out[name] = r.stdout.strip()
    return out


def here(cli=None):
    """Контекст этого процесса: клон текущего каталога, окружение, cli."""
    return resolve(cli or {}, os.environ, clone_binding())


def current():
    return _current.get()


# Поля, что старше файлов настроек, объявлены здесь одним местом (#156):
# config -- нижний слой и сверху не читает, поэтому вписываем сами.
config.attach_context({var: name for name, (_, var) in FIELDS.items()}, current)


@contextlib.contextmanager
def use(ctx):
    """Сделать ctx контекстом процесса на время блока. Окружение дочерних
    процессов -- тоже; на выходе всё как было."""
    token = _current.set(ctx)
    saved = {var: os.environ.get(var) for _, var in FIELDS.values()}
    try:
        for name, (_, var) in FIELDS.items():
            value = getattr(ctx, name)
            if value:
                os.environ[var] = value
        yield ctx
    finally:
        for var, value in saved.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value
        _current.reset(token)
