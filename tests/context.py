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
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import config  # noqa: E402

try:
    from mop import context  # noqa: E402
except ImportError:
    context = None


def main():
    failed = []
    if context is None:
        print("FAIL mop.context is missing\ncontext: FAILED")
        return 1
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
        c = context.resolve(**args)
        if (c.server, c.sources.get("server")) != server or \
                (c.user, c.sources.get("user")) != user:
            failed.append(f"resolve({args}) -> {c}, wanted server {server}, user {user}")

    # Глобальная опция снимается с любого места, остальное argv -- как было.
    for argv, want in [
        (["--server", "a", "list"], ("a", ["list"])),
        (["list", "--server=a"], ("a", ["list"])),
        (["secret", "file", "add", "--server", "a", ".env"], ("a", ["secret", "file", "add", ".env"])),
        (["list"], (None, ["list"])),
    ]:
        got = context.strip_server(argv)
        if got != want:
            failed.append(f"strip_server({argv}) -> {got}, wanted {want}")
    try:
        context.strip_server(["list", "--server"])
        failed.append("a bare --server must be refused")
    except ValueError:
        pass

    # Менеджер ставит контекст и возвращает прежний, в том числе окружение
    # дочерних процессов; config.get видит сервер контекста.
    saved = os.environ.pop("MOP_SERVER_LAN", None)
    try:
        with context.use(context.resolve(cli={"server": "outer"}, env={}, clone={})):
            if config.get("MOP_SERVER_LAN") != "outer" or os.environ.get("MOP_SERVER_LAN") != "outer":
                failed.append("inside use() config.get and the environment must see the context")
            with context.use(context.resolve(cli={"server": "inner"}, env={}, clone={})):
                if config.get("MOP_SERVER_LAN") != "inner":
                    failed.append("a nested use() must win")
            if config.get("MOP_SERVER_LAN") != "outer":
                failed.append("leaving a nested use() must restore the outer context")
        if "MOP_SERVER_LAN" in os.environ or context.current().server is not None:
            failed.append("leaving use() must restore the environment and the context")
        # Пустой контекст не мешает файлам: дефолт установки -- из них.
        with context.use(context.resolve(cli={}, env={}, clone={})):
            if "MOP_SERVER_LAN" in os.environ:
                failed.append("an empty context must not export an empty server")
    finally:
        if saved is not None:
            os.environ["MOP_SERVER_LAN"] = saved

    # ── интеграционная ветка per user (#249) ─────────────────────────────
    # HYPOTHESIS: ветку тикета `mop bug start` заводит от origin/HEAD, а
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
        c = context.resolve(**args)
        got = (getattr(c, "branch", "MISSING"), c.sources.get("branch"))
        if got != want:
            failed.append(f"#249 resolve({args}).branch -> {got}, wanted {want}")
    c = context.resolve(cli={}, env={"MOP_BRANCH": "envdev"}, clone=clone_b)
    if (c.server, c.user) != ("clone.srv", "cloneuser"):
        failed.append(f"#249 the branch layer must leave server and user alone: {c}")
    saved_b = os.environ.pop("MOP_BRANCH", None)
    try:
        from mop.cli import lib
        with context.use(context.resolve(cli={"branch": "dev"}, env={}, clone={})):
            if os.environ.get("MOP_BRANCH") != "dev":
                failed.append("#249 use() must export MOP_BRANCH to child processes")
            if lib.default_branch() != "origin/dev":
                failed.append(f"#249 default_branch under a branch context -> "
                              f"{lib.default_branch()!r}, wanted 'origin/dev'")
        if "MOP_BRANCH" in os.environ:
            failed.append("#249 leaving use() must drop MOP_BRANCH")
        with context.use(context.resolve(cli={}, env={}, clone={})):
            if not lib.default_branch().startswith("origin/"):
                failed.append(f"#249 without a branch context default_branch must ask git: "
                              f"{lib.default_branch()!r}")
    finally:
        if saved_b is not None:
            os.environ["MOP_BRANCH"] = saved_b

    print("\n".join(f"FAIL {l}" for l in failed) if failed else "", end="\n" if failed else "")
    print("context: FAILED" if failed else "context: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
