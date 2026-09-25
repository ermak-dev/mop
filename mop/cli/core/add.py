"""create a puppet: mop add [--llm PROFILE] [git-origin]

Without origin, the origin of the current working copy is used. The name is
picked automatically: <project>-<number>. Nomad decides placement — a puppet
reserves 8 GB from the pool. Silent when the puppet is running; on a
terminal it shows the current step. The new puppet is in mop list.
"""
import os
import time

from mop.cli import lib
from mop import bus, context, llm, puppets


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "destructive", "args": [
    {"name": "origin", "type": "string", "help": "git origin; without it, the origin of the master's working copy"},
    {"name": "llm", "type": "string", "flag": "--llm", "help": "LLM profile"}]}


def main(argv):
    profile, args = lib.parse_llm(argv)
    if len(args) > 1:
        lib.usage(__doc__)
    profile = llm.resolve(profile)
    origin = lib.origin(args[0] if args else None, __doc__)
    project = puppets.project_of(origin)
    # Курица и яйцо: у нового проекта ещё нет пользователя в конфиге NATS, и
    # папет поднимется, но к шине не подключится — прочитается как «агент
    # молчит» на пустом месте. Лучше отказать здесь, чем разбираться там.
    if not lib.project_ready(project):
        lib.usage(f"project {project} isn't on the bus yet.\n"
                  f"Register it on the server: mop project add {origin}")
    p = lib.Progress(project)
    try:
        return _add(origin, project, profile, bool(args), p)
    finally:
        p.clear()


def _add(origin, project, profile, named, p):
    """Долгая команда (#124): на терминале -- текущий шаг, при успехе --
    ничего; отказ и не вставший папет -- ошибкой."""
    p.step("LLM keys to the nodes")
    lib.push_llm_keys(profile)
    # Имя выбирает сервис кластера вместе с регистрацией: спека собирается
    # там же (#80), а выбор имени и есть первая её строка.
    # workspace папета (#133) едет с регистрацией: рабочая копия проекта,
    # вне её -- origin; нет файла -- у папета workspace нет.
    p.step("registering")
    # Ветка мастера (#256): свежий клон папета встаёт на неё, а не на
    # origin/HEAD. Из контекста команды (git config mop.branch, MOP_BRANCH).
    got = bus.call_cluster("add", origin=origin, profile=profile, timeout=30,
                           workspace=lib.workspace_text(origin),
                           branch=context.current().branch)
    name = got["name"]

    p.step(f"{name}: waiting for a node")
    node = None
    for _ in range(120):
        a = bus.call_cluster("alloc", name=name).get("alloc")
        if a:
            node = a["NodeName"]
            if a["ClientStatus"] == "running":
                return 0
            if a["ClientStatus"] == "failed" or puppets.failing(a):
                p.clear()
                lib.fail(f"{name} on {node}: {puppets.failing(a) or 'failed to start'}")
                return 1
            p.step(f"{name}: starting on {node}")
        time.sleep(1)
    p.clear()
    lib.fail(f"{name} is " + ("not placed on any node" if node is None
                              else f"still starting on {node}")
             + " after 2 minutes: mop doctor")
    return 1


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
