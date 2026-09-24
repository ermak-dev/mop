"""update a puppet: mop update <name> [git-origin] [--llm PROFILE] [--fresh]

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
"""
from mop.cli import lib
from mop import bus, llm


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "destructive", "args": [
    {"name": "name", "type": "string", "required": True, "help": "puppet name, pu-<project>-<n>"},
    {"name": "origin", "type": "string", "help": "new git origin; without it, the repository is kept"},
    {"name": "llm", "type": "string", "flag": "--llm", "help": "LLM profile"},
    {"name": "fresh", "type": "boolean", "flag": "--fresh", "help": "come up with a clean session"}]}


def main(argv):
    profile, args = lib.parse_llm(argv)
    fresh = "--fresh" in args
    args = [a for a in args if a != "--fresh"]
    if not 1 <= len(args) <= 2:
        lib.usage(__doc__)
    name = args[0]
    spec = lib.guard(name) or bus.call_cluster("spec", name=name)
    meta = spec.get("meta") or {}
    old, old_llm = meta.get("origin"), llm.of_meta(meta)
    if not old:
        # Джоб без origin в Meta — не папет: ростер их и не показывает.
        lib.usage(f"{name}: no origin in the spec — this isn't a pool puppet")
    origin = args[1] if len(args) > 1 else old
    profile = llm.resolve(profile, old_llm)
    lib.push_llm_keys(profile)
    # История каталога переживает только смену профиля: при смене репозитория
    # врапер пересоздаёт клон, а разговор остался от прежнего проекта — поднять
    # его в чужом репозитории значит выдать папету чужой контекст за свой.
    cont = not fresh and origin == old
    # Спеку собирает сервис кластера: сюда она больше не ездит (#80) —
    # иначе кто угодно с доступом к шине клал бы на узел свою командную
    # строку. Решение «что меняем» остаётся здесь, сборка — там.
    bus.call_cluster("update", name=name, origin=origin, profile=profile,
                     cont=cont, new_origin=origin if origin != old else None,
                     workspace=lib.workspace_text(origin))
    moved = f"{old} [{old_llm}] → {origin} [{profile}]" if origin != old \
        else f"{origin}: {old_llm} → {profile}" if profile != old_llm \
        else f"{origin} [{profile}], unchanged"
    print(f"{name}: {moved}")
    print("puppet is restarting" + (", directory history will come up" if cont
          else " with a clean session" + (" (--fresh)" if fresh else ": repository changed")))



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
