"""Полный PostgreSQL gate foundation и каталога без внешней test-транзакции."""
import sys
import _lib  # noqa: F401 — lifetime временных файлов
import hermetic  # noqa: F401 — до framework и mop
from _controller_fixture import main


if __name__ == '__main__':
    sys.exit(main(['_controller_cases', '_controller_restore_cases', '_catalog_cases']))
