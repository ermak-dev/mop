"""update a puppet: mop update <name> [git-origin] [--fresh] [--force]

Changes what's named and keeps the rest: without origin the puppet stays on
its repository. By default the claude that comes up resumes the directory's
last conversation; --fresh brings it up with a clean session. An allocation
restart doesn't reread the spec, and treatment is a restart — a treated
puppet isn't obliged to come back into the context it got stuck on.

Another master's puppet (work in its clone, or dispatched minutes ago) is
refused with that master's name; --force acts anyway and says whose it was.
"""
from mop.cli import lib
from mop.cli.core import _common
from mop.common import bus, context, ops
from mop.common.domain import JobMeta


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "destructive", "args": [
    {"name": "name", "type": "string", "required": True, "help": "puppet name, pu-<project>-<n>"},
    {"name": "origin", "type": "string", "help": "new git origin; without it, the repository is kept"},
    {"name": "fresh", "type": "boolean", "flag": "--fresh", "help": "come up with a clean session"},
    {"name": "force", "type": "boolean", "flag": "--force",
     "help": "act on a puppet another master leads; the answer names whom"}]}


def main(argv):
    fresh = "--fresh" in argv
    args, force = lib.parse_named([a for a in argv if a != "--fresh"], __doc__, most=2)
    name = args[0]
    # Перила мастер-шелла уже отдали спеку (чей папет): не спрашивать
    # вторично, тот же ответ едет в общую операцию. Вне проекта перил нет
    # (guard=None) -- спеку спрашиваем сами, как до #459: прежний репозиторий
    # нужен ЗДЕСЬ, до workspace_text.
    spec = lib.guard(name) or bus.call_cluster("spec", name=name)
    # Прежний репозиторий -- из спеки (#503): без origin папет остаётся на
    # нём (контракт до #459). workspace_text строку и ждёт -- None звал
    # manifest.fetch(None) и падал TypeError.
    old = JobMeta.from_meta(spec.get("meta")).origin
    if len(args) == 1 and not old:
        lib.usage(f"{name}: no origin in the spec — this isn't a pool puppet")
    origin = args[1] if len(args) > 1 else old
    text, sent = _common.workspace_text(origin)
    v = ops.update(bus.call_cluster, name,
                   origin=origin if len(args) > 1 else None,
                   fresh=fresh, workspace=(text, sent),
                   force=force, login=bus.login(),
                   branch=context.current().branch, spec=spec,
                   bootstrap_wait=_common.BOOTSTRAP_WAIT)
    # Порядок прежний (#263): чью аренду прошёл force -- строкой до
    # «что уехало в bootstrap», затем итог.
    lib.note(name, v)
    print(_common.sent_line(sent), flush=True)
    p = lib.Progress(name)
    try:
        return _common.render(v, p, name)
    finally:
        p.clear()


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
