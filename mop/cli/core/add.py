"""create a puppet: mop add [git-origin]

Without origin, the origin of the current working copy is used. The name is
picked automatically: <project>-<number>; placement over the pool and the
8 GB reservation are not chosen here. Silent when the puppet is running; on
a terminal it shows the current step. The new puppet is in mop list.
Use mop models to see model IDs offered by the selected proxy.
"""

from mop.cli import lib
from mop.cli.core import _common
from mop.common import bus, context, ops, puppets


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "destructive", "args": [
    {"name": "origin", "type": "string", "help": "git origin; without it, the origin of the master's working copy"},
    ]}


def main(argv):
    if len(argv) > 1:
        lib.usage(__doc__)
    origin = lib.origin(argv[0] if argv else None, __doc__)
    project = puppets.project_of(origin)
    # Курица и яйцо: у нового проекта ещё нет пользователя в конфиге NATS, и
    # папет поднимется, но к шине не подключится — прочитается как «агент
    # молчит» на пустом месте. Лучше отказать здесь, чем разбираться там.
    if not lib.project_ready(project):
        lib.usage(f"project {project} isn't on the bus yet.\n"
                  f"Register it on the server: mop project add {origin}")
    p = lib.Progress(project)
    try:
        return _add(origin, project, bool(argv), p)
    finally:
        p.clear()


def _add(origin, project, named, p):
    """Долгая команда (#124): на терминале -- текущий шаг, при успехе --
    ничего; отказ и не вставший папет -- ошибкой. Workflow -- общая
    операция (#459): командлет строит вход и рисует итог."""
    p.step("registering")
    text, sent = _common.workspace_text(origin)
    print(_common.sent_line(sent), flush=True)
    v = ops.add(bus.call_cluster, origin, (text, sent),
                branch=context.current().branch,
                step=lambda s: p.step(s), place_wait=120)
    return _common.render(v, p)


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
