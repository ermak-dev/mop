"""mop profile: show the pool role, project and integration branch

The role comes from bus credentials, not the presence of MCP tools. An unset
integration branch is reported as unset; it is not inferred from origin/HEAD.
"""
from mop.cli import lib
from mop.common import bus, busnames, context


MCP = {"annotations": "readonly", "args": []}


def profile_name(master, project):
    """Роль по пользователю шины и проекту, общая с `mop mcp --check`."""
    if not master:
        return "node"
    return "operator" if project == bus.ADMIN else f"master of project {project}"


def main(argv):
    if argv:
        lib.usage(__doc__)
    user = bus.config().get("user")
    if not user:
        raise RuntimeError("no bus credentials")
    role = profile_name(not busnames.is_puppet(user), bus.PROJECT)
    branch = context.current().branch or "not set"
    print(f"profile {role}; integration branch {branch}")
