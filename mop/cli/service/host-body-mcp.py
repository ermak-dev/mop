"""mop host-body-mcp --scope unit --deadline UTC: serve installed body-only standby MCP

Internal isolated entry for the fixed host lifecycle broker. The immutable
installation derives the private bootstrap/context siblings from the exact scope.
No caller-supplied path, token, API origin, environment or command is accepted.
A checkout or ordinary non-isolated Python invocation refuses before handoff.
"""


def main(argv):
    from mop.client.body_entry import main as installed_main
    return installed_main(argv)
