"""mop profile: show the pool role, project and integration branch

The role comes from bus credentials, not the presence of MCP tools. An unset
integration branch is reported as unset; it is not inferred from origin/HEAD.
A role principal is profiled by its bundle's role name — a grant is a puppet,
not a master of anything.
"""
from mop.cli import lib
from mop.client import channel
from mop.common import bus, busnames, context


MCP = {"annotations": "readonly", "node_verbs": [], "cluster_verbs": [], "args": []}


def profile_name(master, project):
    """Роль по пользователю шины и проекту, общая с `mop mcp --check`."""
    if not master:
        return "node"
    return "operator" if project == bus.ADMIN else f"master of project {project}"


def grant_profile(environ=None):
    """Строка профиля принципала роли (#585): имя роли -- из бандла тела
    (MOP_SOUL_DIR), не из кредов: имя на шине -- публичный id без роли.
    Без бандла -- честное «unknown»: тело не подтвердило, кем оно есть,
    и называть его ролью наугад нельзя."""
    bundle, _ = channel.own_bundle(environ)
    return f"role {bundle.role}" if bundle else "role unknown (no bundle)"


def main(argv):
    if argv:
        lib.usage(__doc__)
    user = bus.config().get("user")
    if not user:
        raise RuntimeError("no bus credentials")
    who = channel.actor(user)
    if who == channel.GRANT:
        role = grant_profile()
    else:
        # Прежний шов «мастер/узел»: не-puppet до #579 и не человек теперь
        # расходятся сами (channel.actor), здесь остаётся вид профиля.
        role = profile_name(who == channel.HUMAN, bus.PROJECT)
    branch = context.current().branch or "not set"
    print(f"profile {role}; integration branch {branch}")
