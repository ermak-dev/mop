"""mop mcp [--check] [--project-id UUID] [--node-id UUID] [--client-id UUID]: authenticated management tools

Uses only transient HTTPS context and the server's current scoped action
catalogue. --check queries that catalogue and reports the private inbox as
unavailable; it does not register an endpoint or confirm native delivery.
No scope exposes only authenticated identity/health diagnostics, not all actions.
Management tools invoke the bound commandlets themselves. The private inbox,
agent discovery, reports and idle notifications remain unavailable.

Installed bodies may instead select --body-standby --body-bootstrap PATH
--body-context PATH --body-deadline UTC. These options require two distinct
absolute sibling paths in an owned private directory and an explicit UTC
startup deadline no more than 300 seconds ahead. Files may initially be absent:
initialize succeeds with no identity or tools; no commandlet runs before binding.
The verified installed root publishes the common BodyBootstrap descriptor after
producer ACK and the separate ManagementContext only after grant activation.
Only bounded private no-follow single-link files and certificate bytes are read,
never a CA pathname or permissions from the descriptor. A successful verified
HTTPS Agent catalogue enables discovery and tools/list_changed; each later call
refreshes current permissions and refuses expired or changed identity/scope.
Body mode ignores ambient HTTPS context, cannot combine with --check, ordinary
scope selectors or explicit global HTTPS selectors, and exits unsuccessfully if
the handoff deadline expires. This client does not supply the root issuer or
prove physical body binding; the private inbox remains unavailable.
"""
from mop.cli import _control


def main(argv=None):
    return _control.invoke('mcp', argv or [])
