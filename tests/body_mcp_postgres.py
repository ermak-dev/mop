"""Настоящий DRF/PostgreSQL active Agent -> body stdio #748."""
import sys
import _lib  # noqa: F401
import hermetic  # noqa: F401
from _controller_fixture import main

if __name__ == '__main__':
    sys.exit(main(['_body_mcp_cases']))
