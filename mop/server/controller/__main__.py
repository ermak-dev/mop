"""`python -m mop.server.controller <action>`: вход юнита в venv установки.

Установка рендерит `ExecStart=<venv>/bin/python -m mop.server.controller ...`
(#807): интерпретатор объявляет установка, а не литерал машины. Сама логика
остаётся у командлета `mop server controller` -- здесь только передача argv,
второй реализации нет.
"""
import sys

from mop.cli.server.controller import main

sys.exit(main(sys.argv[1:]))
