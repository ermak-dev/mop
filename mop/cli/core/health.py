"""mop health: query authenticated HTTPS controller readiness

Select the server with --api-origin https://host and optional --api-ca PATH,
or transient MOP_API_ORIGIN/MOP_API_CA. MOP_API_TOKEN supplies the Django token;
it is never an argv option and no connection.json or git config is read.
The response describes observed controller readiness, not puppet health.
"""
from mop.cli import _control

MCP = {"annotations": "readonly", "args": []}


def main(argv):
    return _control.invoke('health', argv)
