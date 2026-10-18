"""mop mcp [--check] [--project-id UUID] [--node-id UUID] [--client-id UUID]: authenticated management tools

Uses only transient HTTPS context and the server's current scoped action
catalogue. --check queries that catalogue and reports the private inbox as
unavailable; it does not register an endpoint or confirm native delivery.
No scope exposes only authenticated identity/health diagnostics, not all actions.
Management tools invoke the bound commandlets themselves. The private inbox,
agent discovery, reports and idle notifications remain unavailable.
"""
from mop.cli import _control


def main(argv=None):
    return _control.invoke('mcp', argv or [])
