"""mop profile: query the authenticated controller identity over HTTPS

Returns the actual principal kind and owner, never a fabricated live session
address or human authority inferred from a role name. Missing server action
permissions remain unavailable, not an all-tools grant. No local credential,
role-bundle, connection.json or git config store is read.

Select --api-origin and optional --api-ca, or explicit transient MOP_API_ORIGIN,
MOP_API_CA and MOP_API_TOKEN. The token is never a command-line option or output.
"""
from mop.cli import _control

MCP = {"annotations": "readonly", "actions": [], "args": []}


def profile_name(master, project):
    """Старый formatter для исторических source checks, не источник authority."""
    if not master:
        return 'node'
    return 'operator' if project == 'admin' else f'master of project {project}'


def main(argv):
    return _control.invoke('profile', argv)
