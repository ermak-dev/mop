#!/usr/bin/env python3
"""Проверка контекста сервера и пользователя без пула: python3 tests/context.py

Контекст (#131) -- на какой сервер и под каким именем идёт команда. Слои:
рабочая копия (git config mop.server, mop.user), поверх окружение, поверх
командная строка; нет ни одного -- дефолт установки из файлов. Здесь --
разрешение слоёв, снятие глобального --server с argv и контекстный
менеджер; git и шина -- на живой машине.
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched_env  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import config  # noqa: E402

try:
    from mop.common import context  # noqa: E402
except ImportError:
    context = None


def main():
    c = Checks()
    if not c.check("mop.context exists", context is not None):
        return c.report("context")
    # HYPOTHESIS (#131): сервер клона знал только config.get, логин -- только
    # join, а --server был лишь у join. SOLUTION: один контекст по слоям для
    # всех команд. STATUS: FIXED — see #131
    clone = {"server": "clone.srv", "user": "cloneuser"}
    env = {"MOP_SERVER_LAN": "env.srv", "MOP_BUS_USER": "envuser"}
    cases = [
        (dict(cli={}, env={}, clone=clone), ("clone.srv", "clone"), ("cloneuser", "clone")),
        (dict(cli={}, env=env, clone=clone), ("env.srv", "env"), ("envuser", "env")),
        (dict(cli={"server": "cli.srv", "user": "cliuser"}, env=env, clone=clone),
         ("cli.srv", "cli"), ("cliuser", "cli")),
        (dict(cli={}, env={}, clone={}), (None, None), (None, None)),
        # Слои -- по полю: сервер из командной строки не отменяет логина клона.
        (dict(cli={"server": "cli.srv"}, env={}, clone=clone),
         ("cli.srv", "cli"), ("cloneuser", "clone")),
    ]
    for args, server, user in cases:
        r = context.resolve(**args)
        c.check(f"resolve({args})",
                not ((r.server, r.sources.get("server")) != server or
                     (r.user, r.sources.get("user")) != user),
                f"-> {r}, wanted server {server}, user {user}")

    # Глобальная опция снимается с любого места, остальное argv -- как было.
    for argv, want in [
        (["--server", "a", "list"], ("a", ["list"])),
        (["list", "--server=a"], ("a", ["list"])),
        (["secret", "file", "add", "--server", "a", ".env"], ("a", ["secret", "file", "add", ".env"])),
        (["list"], (None, ["list"])),
    ]:
        c.expect(f"strip_server({argv})", context.strip_server(argv), want)
    try:
        context.strip_server(["list", "--server"])
        c.fail("a bare --server must be refused")
    except ValueError:
        pass

    # Менеджер ставит контекст и возвращает прежний, в том числе окружение
    # дочерних процессов; config.get видит сервер контекста.
    with patched_env(MOP_SERVER_LAN=None):
        with context.use(context.resolve(cli={"server": "outer"}, env={}, clone={})):
            c.check("inside use() config.get and the environment must see the context",
                    not (config.get("MOP_SERVER_LAN") != "outer"
                         or os.environ.get("MOP_SERVER_LAN") != "outer"))
            with context.use(context.resolve(cli={"server": "inner"}, env={}, clone={})):
                c.expect("a nested use() must win", config.get("MOP_SERVER_LAN"), "inner")
            c.expect("leaving a nested use() must restore the outer context",
                     config.get("MOP_SERVER_LAN"), "outer")
        c.check("leaving use() must restore the environment and the context",
                not ("MOP_SERVER_LAN" in os.environ or context.current().server is not None))
        # Пустой контекст не мешает файлам: дефолт установки -- из них.
        with context.use(context.resolve(cli={}, env={}, clone={})):
            c.check("an empty context must not export an empty server",
                    "MOP_SERVER_LAN" not in os.environ)

    # ── интеграционная ветка per user (#249) ─────────────────────────────
    # HYPOTHESIS: ветку тикета `mop dev bug start` заводит от origin/HEAD, а
    # мастер называет папету цель landing по правилам проекта; «мою» ветку
    # задать негде: ветка по умолчанию в GitLab одна на репозиторий.
    # SOLUTION: третье поле контекста -- branch: git config mop.branch клона
    # (или --global), поверх MOP_BRANCH окружения, поверх cli; use() экспортирует
    # его дочерним процессам (сессии мастера), lib.default_branch берёт
    # origin/<ветка> из контекста и лишь без неё спрашивает git.
    # STATUS: FIXED — see #249
    clone_b = {"server": "clone.srv", "user": "cloneuser", "branch": "dev"}
    for args, want in [
        (dict(cli={}, env={}, clone=clone_b), ("dev", "clone")),
        (dict(cli={}, env={"MOP_BRANCH": "envdev"}, clone=clone_b), ("envdev", "env")),
        (dict(cli={"branch": "clidev"}, env={"MOP_BRANCH": "envdev"}, clone=clone_b),
         ("clidev", "cli")),
        (dict(cli={}, env={}, clone={}), (None, None)),
        # Поле независимое: ветка окружения не отменяет сервера и логина клона.
        (dict(cli={}, env={"MOP_BRANCH": "envdev"}, clone=clone_b), ("envdev", "env")),
    ]:
        r = context.resolve(**args)
        got = (getattr(r, "branch", "MISSING"), r.sources.get("branch"))
        c.expect(f"#249 resolve({args}).branch", got, want)
    r = context.resolve(cli={}, env={"MOP_BRANCH": "envdev"}, clone=clone_b)
    c.expect("#249 the branch layer must leave server and user alone",
             (r.server, r.user), ("clone.srv", "cloneuser"))
    with patched_env(MOP_BRANCH=None):
        from mop.cli import lib
        with context.use(context.resolve(cli={"branch": "dev"}, env={}, clone={})):
            c.expect("#249 use() must export MOP_BRANCH to child processes",
                     os.environ.get("MOP_BRANCH"), "dev")
            c.expect("#249 default_branch under a branch context",
                     lib.default_branch(), "origin/dev")
        c.check("#249 leaving use() must drop MOP_BRANCH", "MOP_BRANCH" not in os.environ)
        with context.use(context.resolve(cli={}, env={}, clone={})):
            c.check("#249 without a branch context default_branch must ask git",
                    lib.default_branch().startswith("origin/"), repr(lib.default_branch()))

    # ── #457: изолированный контекст общих операций ─────────────────────
    # HYPOTHESIS (#457): ContextVar в context.use сопровождается записью
    # os.environ, а context.here читает cwd процесса. Общая операция,
    # позванная из двух потоков (CLI и HTTP-worker), перехлёстывается:
    # окружение -- вход отдельного CLI-процесса, но не хранилище состояния
    # HTTP-запроса; cwd -- процессная величина. Механически обернуть
    # HTTP-handler менеджером use() недостаточно.
    # SOLUTION: isolated(ctx) -- контекст без побочных эффектов: ни
    # os.environ, ни cwd; нижний вызов (bus/config) принимает контекст
    # явно, worker получает ровно переданный контекст аргументом, не
    # переносом ContextVar в поток. Immutable-контекст без изменяемых
    # вложенных словарей: sources -- mapping-proxy.
    # STATUS: FIXED — see #457
    import threading

    ctx = context.resolve(cli={"server": "a.srv", "branch": "dev"}, env={}, clone={})
    c.check("#457 sources must not be a mutable dict",
            not isinstance(ctx.sources, dict), type(ctx.sources).__name__)
    try:
        ctx.sources["server"] = "b.srv"
        c.fail("#457 sources must be immutable", "assignment went through")
    except TypeError:
        pass

    # Окружение и cwd неизменны после isolated -- успеха и исключения.
    env_before = dict(os.environ)
    cwd_before = os.getcwd()
    caught = False
    try:
        with context.isolated(context.resolve(cli={"server": "iso.srv"}, env={}, clone={})):
            c.check("#457 isolated must set current() without touching os.environ",
                    not (context.current().server != "iso.srv"
                         or os.environ != env_before or os.getcwd() != cwd_before))
            raise RuntimeError("boom")
    except RuntimeError:
        caught = True
    c.check("#457 isolated must restore everything even on exception",
            not (not caught or os.environ != env_before or os.getcwd() != cwd_before
                 or context.current().server is not None))

    # Два одновременно исполняемых контекста с разными actor/project/branch и
    # подставным транспортом: у нижнего вызова нет чужих значений. Контекст
    # передаётся ЯВНО: worker не рассчитывает на перенос ContextVar в поток.
    seen = {}
    barrier = threading.Barrier(2)

    def fake_transport(label, actor, project, branch):
        barrier.wait()
        seen[label] = (context.current().server, actor, project, branch)
        return {"ok": True}

    def run(label, ictx):
        with context.isolated(ictx):
            context.call_in_context(ictx, fake_transport, label,
                                    actor="actor-" + label, project=label,
                                    branch=ictx.branch)

    one = context.resolve(cli={"server": "one.srv", "branch": "dev-one"}, env={}, clone={})
    two = context.resolve(cli={"server": "two.srv", "branch": "dev-two"}, env={}, clone={})
    t1 = threading.Thread(target=run, args=("p1", one))
    t2 = threading.Thread(target=run, args=("p2", two))
    t1.start(); t2.start(); t1.join(); t2.join()
    c.check("#457 two parallel contexts must not leak into each other",
            not (seen.get("p1") != ("one.srv", "actor-p1", "p1", "dev-one")
                 or seen.get("p2") != ("two.srv", "actor-p2", "p2", "dev-two")),
            seen)

    # Креды шины выбираются контекстом, а не процессом: другой сервер --
    # другой файл, кэш одного соединения под ногами не путает установки.
    c.check("#457 bus credentials must key on the context, not the process",
            context.bus_file(one) != context.bus_file(two),
            (context.bus_file(one), context.bus_file(two)))

    # Явный env для реального дочернего процесса -- копия, не мутация.
    child = context.child_env(one, MOP_EXTRA="x")
    c.check("#457 child_env builds a copy without mutating os.environ",
            not (child is os.environ or child.get("MOP_SERVER_LAN") != "one.srv"
                 or "MOP_SERVER_LAN" in os.environ or child.get("MOP_EXTRA") != "x"))

    return c.report("context")


if __name__ == "__main__":
    sys.exit(main())
