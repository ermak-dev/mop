"""Узкий PostgreSQL gate реестра отказов #691; cases также входят в controller.py."""
import sys
import _lib  # noqa: F401 — lifetime временных файлов
import hermetic  # noqa: F401 — до framework и mop
from _controller_fixture import main


if __name__ == '__main__':
    sys.exit(main(['_controller_error_cases']))
