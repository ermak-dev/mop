"""update a puppet: mop update <name> [git-origin] [--llm PROFILE] [--fresh] [--force]

Changes what's named and keeps the rest: without origin the puppet stays on
its repository, without --llm it stays on its profile. Switching the
repository must not silently drop the profile back to default, and vice
versa.

By default the claude that comes up resumes the directory's last
conversation, so switching profiles moves work already in progress onto a
different model. That's a one-off allowance: an allocation restart doesn't
reread the spec, and treatment is a restart — a treated puppet isn't obliged
to come back into the context it got stuck on.
--fresh brings it up with a clean session.

Another master's puppet (work in its clone, or dispatched minutes ago) is
refused with that master's name; --force acts anyway and says whose it was.
"""
from mop.cli import lib
from mop.cli.core import _common
from mop.common import bus, context, llm


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "destructive", "args": [
    {"name": "name", "type": "string", "required": True, "help": "puppet name, pu-<project>-<n>"},
    {"name": "origin", "type": "string", "help": "new git origin; without it, the repository is kept"},
    {"name": "llm", "type": "string", "flag": "--llm", "help": "LLM profile"},
    {"name": "fresh", "type": "boolean", "flag": "--fresh", "help": "come up with a clean session"},
    {"name": "force", "type": "boolean", "flag": "--force",
     "help": "act on a puppet another master leads; the answer names whom"}]}


def main(argv):
    profile, args = _common.parse_llm(argv)
    fresh = "--fresh" in args
    args, force = lib.parse_named([a for a in args if a != "--fresh"], __doc__, most=2)
    name = args[0]
    spec = lib.guard(name) or bus.call_cluster("spec", name=name)
    meta = spec.get("meta") or {}
    old, old_llm = meta.get("origin"), llm.of_meta(meta)
    if not old:
        # Джоб без origin в Meta — не папет: ростер их и не показывает.
        lib.usage(f"{name}: no origin in the spec — this isn't a pool puppet")
    origin = args[1] if len(args) > 1 else old
    profile = llm.resolve(profile, old_llm)
    _common.push_llm_keys(profile)
    # История каталога переживает только смену профиля: при смене репозитория
    # врапер пересоздаёт клон, а разговор остался от прежнего проекта — поднять
    # его в чужом репозитории значит выдать папету чужой контекст за свой.
    cont = not fresh and origin == old
    # Спеку собирает сервис кластера: сюда она больше не ездит (#80) —
    # иначе кто угодно с доступом к шине клал бы на узел свою командную
    # строку. Решение «что меняем» остаётся здесь, сборка — там.
    got = bus.call_cluster("update", name=name, origin=origin, profile=profile,
                           cont=cont, new_origin=origin if origin != old else None,
                           workspace=_common.workspace_text(origin),
                           owner=bus.login(), force=force,
                           branch=context.current().branch)
    # На успехе молчим (#179), как restart: MCP ответит модели `done`, а эхо
    # параметров повторяло то, что человек только что набрал сам. Чью аренду
    # прошёл force -- называем (#40).
    lib.note(name, got)



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
