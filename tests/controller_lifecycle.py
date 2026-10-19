"""Обязательный owned-management gate #668: purpose, ownership и runtime limits.

Отдельный процесс сохраняет задержку DROP до teardown; другие test cases с
patch.stopall не могут раньше снять её и сделать регрессию пустой.
"""
import sys
import _lib  # noqa: F401 — lifetime временных файлов
import hermetic  # noqa: F401 — до framework и mop
from _controller_fixture import main


if __name__ == '__main__':
    sys.exit(main(['_controller_lifecycle_cases.OwnedLifecycle']))
