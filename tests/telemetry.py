"""Узкий PostgreSQL gate #641; полный CI controller.py включает те же cases."""
import sys
import _lib  # noqa: F401
import hermetic  # noqa: F401
from _controller_fixture import main


if __name__ == '__main__':
    sys.exit(main(['_telemetry_cases']))
