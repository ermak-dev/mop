"""Неустранённый actual maximum-ref deadline counterexample после #751 byte fix."""
import sys
import _lib  # noqa: F401
import hermetic  # noqa: F401
from _controller_fixture import main

if __name__ == '__main__':
    sys.exit(main(['_bootstrap_transfer_deadline_cases']))
