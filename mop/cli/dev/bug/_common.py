"""Общее глаголам трекера: тело задачи и разбор аргументов."""
import functools
import sys

from mop.cli.dev import _common as dev_common


def read_body(text, path):
    """Тело задачи: --body-file, аргумент или stdin. Через файл и stdin — ради
    бэктиков и переносов: шелл их съедает, а тело задачи без них не написать."""
    if path == "-":
        # `-` -- stdin, как у текста комментария (#270): прежде его искали
        # файлом и падали трассировкой.
        return sys.stdin.read().strip()
    if path:
        # encoding явно: тела задач по-русски (CLAUDE.md), а кодировка по
        # умолчанию берётся из локали процесса — то есть зависит от машины, с
        # которой команду позвали. На локали без UTF-8 чтение падало
        # UnicodeDecodeError, и это про машину, а не про текст.
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError as e:
            # Нет файла, каталог, нет прав -- отказ строкой (#270), не трасса.
            sys.exit(f"--body-file {path}: {e.strerror or e}")
    if text and text != "-":
        return text.strip()
    return sys.stdin.read().strip()


# Одна фабрика на обе группы (#263): prog -- `mop dev bug <глагол>`.
parser = functools.partial(dev_common.parser, "bug")
