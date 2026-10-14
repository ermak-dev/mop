"""recreate a puppet on a clean working copy: mop recycle <name> [--force]

Unsaved changes are wiped by restoring from git, the target directory is
removed entirely (the first build after a recycle is slow). The clone
itself isn't recloned. The puppet's workspace (.mop/bootstrap.yaml) comes
from the working copy this runs in, or from the origin elsewhere. A role
principal recycles without one: the server keeps the approved policy.

Another master's puppet (work in its clone, or dispatched minutes ago) is
refused with that master's name; --force acts anyway and says whose it was.
"""
from mop.cli import lib
from mop.cli.core import _common
from mop.common import bus, context, puppets


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "destructive", "node_verbs": ['wipe'], "cluster_verbs": ['spec', 'alloc', 'delete', 'update', 'bootstrap_result'], "args": [
    {"name": "name", "type": "string", "required": True, "help": "puppet name, pu-<project>-<n>"},
    {"name": "force", "type": "boolean", "flag": "--force",
     "help": "act on a puppet another master leads; the answer names whom"}]}


def main(argv):
    name, force = lib.named(argv, __doc__)
    # workspace -- из рабочей копии, откуда зовут (#133): удалённый файл
    # снимается и на сервере. Ветка мастера (#257) -- тоже её, из контекста
    # команды (#367): библиотека окружение не читает. Грант-мастер (#618)
    # рабочей копии не читает вовсе: рецикл без неё (workspace_of=None,
    # контракт `mop server gc`) -- сервер оставляет положенную политику,
    # текст от принципала он отвергает (#582).
    grant = bus.principal() is not None
    if grant:
        _route_guard(name)
    p = lib.Progress(name)
    p.step("stopping, resetting the clone, wiping target")
    try:
        r = puppets.recycle(name, force=force,
                            workspace_of=None if grant else _common.workspace_text,
                            branch=None if grant else context.current().branch)
    finally:
        p.clear()
    lib.note(name, r)
    if r.get("bootstrap_sent"):
        print(_common.sent_line(r["bootstrap_sent"]), flush=True)
    p = lib.Progress(name)
    try:
        return _common.report_bootstrap(name, r, p)
    finally:
        p.clear()


def _route_guard(name):
    """Предзапрос маршрута рецикла для принципала (#618-ревью): снести
    джоб может и урезанная роль (delete), а вернуть к жизни -- нет (update,
    wipe), и останов на полпути теряет папета. Проверка -- тем же
    инструментом, что режет каталог инструментов модели (channel.own_bundle
    + tools_for по объявлению ЭТОГО командлета): нового API прав нет,
    власть остаётся у сервера; человек пути не касается."""
    from mop.client import channel
    bundle, why = channel.own_bundle()
    if bundle is None:
        raise RuntimeError(f"{name}: cannot verify this role's route — {why}; "
                           "a recycle stops the job before it can be "
                           "re-registered, refusing until the bundle is back")
    caps = bundle.manifest.capabilities
    if "recycle" not in channel.tools_for(channel.GRANT, caps, {"recycle": MCP}):
        lack = [v for v in MCP["node_verbs"]
                if v not in caps.node_verbs]
        lack += [v for v in MCP["cluster_verbs"]
                 if v not in caps.cluster_verbs]
        raise RuntimeError(f"{name}: this role's route cannot finish a "
                           f"recycle — its bundle lacks {', '.join(lack)}; "
                           "the server stays the authority, but a stopped "
                           "job would stay stopped — nothing is touched")


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
