"""Узкий PostgreSQL gate proxy consumer; cases также входят в controller.py."""
import sys
import _lib  # noqa: F401
import hermetic  # noqa: F401
from _controller_fixture import main


if __name__ == '__main__':
    sys.exit(main(['_proxy_usage_cases']))
