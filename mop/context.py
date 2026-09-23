"""Контекст команды: на какой сервер и под каким именем она идёт (#131).

Слои, каждый следующий перекрывает предыдущий по своему полю:

    рабочая копия   git config mop.server / mop.user -- привязка клона (#125)
    окружение       MOP_SERVER_LAN / MOP_BUS_USER
    командная строка  глобальный --server у любой команды, логин у join

Ни одного слоя -- поле пусто, и config.get берёт дефолт установки из файлов
(node.env, .env). Пароли в контекст не входят: они у человека на сервере, в
~/.config/mop/servers/<адрес>/, один на все проекты.

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

# Поле контекста -> (ключ git config, переменная окружения).
FIELDS = {"server": ("mop.server", "MOP_SERVER_LAN"),
          "user": ("mop.user", "MOP_BUS_USER")}


@dataclass(frozen=True)
class Context:
    server: str = None
    user: str = None
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
                   sources=sources)


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
