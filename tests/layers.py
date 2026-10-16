"""Проверка слоёв и предметного графа; scanner общий для regression checks."""
import sys
import _lib  # noqa: F401 — общий lifetime временных файлов
from _layers import main

if __name__ == '__main__':
    sys.exit(main())
