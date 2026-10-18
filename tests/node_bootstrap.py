"""PostgreSQL gate private freshNode enrollment/export #722."""
import sys
import _lib  # noqa: F401
import hermetic  # noqa: F401
from _controller_fixture import main

if __name__ == '__main__':
    sys.exit(main(['_node_bootstrap_cases']))
