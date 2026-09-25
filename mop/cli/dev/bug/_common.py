"""Общее глаголам трекера: тело задачи и разбор аргументов."""
import argparse
import sys


def read_body(text, path):
    """Тело задачи: --body-file, аргумент или stdin. Через файл и stdin — ради
    бэктиков и переносов: шелл их съедает, а тело задачи без них не написать."""
    if path:
        # encoding явно: тела задач по-русски (CLAUDE.md), а кодировка по
        # умолчанию берётся из локали процесса — то есть зависит от машины, с
        # которой команду позвали. На локали без UTF-8 чтение падало
        # UnicodeDecodeError, и это про машину, а не про текст.
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    if text and text != "-":
        return text.strip()
    return sys.stdin.read().strip()


def parser(verb):
    """argparse глагола без встроенной справки: usage — докстринг модуля."""
    return argparse.ArgumentParser(prog=f"mop dev bug {verb}", add_help=False)
