"""Цвета терминала и отказ строкой: один раз для mop.cli.lib и для
сборки страницы. Только stdlib: задача web:dist идёт в образе node, где
питонских библиотек нет, а lib на верхнем уровне импортирует шину.
В mop/cli, а не в mop/common: это печать, а библиотека молчит; пакет
mop.cli на верхнем уровне берёт только stdlib, и шина не приезжает."""
# Сборка без шины -- #302; одно определение на двоих -- #320. Номера -- здесь,
# а не в докстринге: докстринги mop/cli проверяются как справка (#255).
import sys

# Цвета — для `mop server deploy`, у которого прогон длинный и заголовки
# разделов нужны глазу.
_RED, _GREEN, _BOLD, _NC = "\033[0;31m", "\033[0;32m", "\033[1m", "\033[0m"


def section(text):
    print(f"\n{_BOLD}{text}{_NC}", flush=True)


def fail(text):
    print(f"{_RED}{text}{_NC}", file=sys.stderr, flush=True)


def ok(text):
    print(f"{_GREEN}{text}{_NC}", flush=True)


def usage(doc):
    sys.exit(doc.strip())
