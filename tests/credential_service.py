"""Настоящий PostgreSQL и private socket credential use cases #637."""
import sys
import _lib  # noqa: F401
import hermetic  # noqa: F401
from _controller_fixture import main

if __name__ == '__main__':
    sys.exit(main(['_credential_cases', '_credential_binding_cases', '_launch_delivery_cases']))
