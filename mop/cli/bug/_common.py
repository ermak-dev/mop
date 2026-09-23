"""Общее глаголам трекера: тело задачи и разбор аргументов."""
import argparse
import sys


def read_body(text, path):
    """Тело задачи: --body-file, аргумент или stdin. Через файл и stdin — ради
    бэктиков и переносов: шелл их съедает, а тело задачи без них не написать."""
    if path:
        with open(path) as f:
            return f.read().strip()
    if text and text != "-":
        return text.strip()
    return sys.stdin.read().strip()


def parser(verb):
    """argparse глагола без встроенной справки: usage — докстринг модуля."""
    return argparse.ArgumentParser(prog=f"mop bug {verb}", add_help=False)
