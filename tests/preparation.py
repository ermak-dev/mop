"""PostgreSQL gate подготовки: отдельная БД, без старой установки."""
import sys
import _lib  # noqa: F401 — lifetime временных файлов
import hermetic  # noqa: F401 — до framework
from _controller_fixture import main

if __name__ == '__main__':
    sys.exit(main(['_preparation_cases']))
