"""mop diagnose: offline check of the HTTPS controller context

Prints one JSON object, with no network and no writes: whether the next
remote command has a server context, its origin, where the credential comes
from and which CA is trusted. Without a context the answer is ready false and
names the missing transient variables (MOP_API_ORIGIN, MOP_API_TOKEN) -- that
is information, exit code 0. A malformed context refuses with exit code 1.

Select --api-origin and optional --api-ca, or transient MOP_API_ORIGIN,
MOP_API_CA and MOP_API_TOKEN. No connection.json, .env or git config is read;
the token is never shown.
"""
from mop.cli import _control

MCP = {"annotations": "readonly", "actions": [], "args": []}


def main(argv):
    return _control.invoke('diagnose', argv)
