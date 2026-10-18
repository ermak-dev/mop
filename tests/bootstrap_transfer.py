"""PG17 gate finite transfer budgets (#751), без live pool и provisioning I/O."""
import sys
import _lib  # noqa: F401
import hermetic  # noqa: F401
from _controller_fixture import main


if __name__ == '__main__':
    sys.exit(main(['_bootstrap_transfer_cases']))
