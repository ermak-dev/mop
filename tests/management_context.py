"""Узкий настоящий PostgreSQL/socket/HTTPS gate management handoff #742."""
import sys
import _lib  # noqa: F401
import hermetic  # noqa: F401
from _controller_fixture import main

if __name__ == '__main__':
    sys.exit(main(['_management_context_cases']))
