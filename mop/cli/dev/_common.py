"""Общее у групп разработчика (bug, ci): разбор аргументов."""
import argparse


def parser(group, verb):
    """argparse глагола без встроенной справки: usage — докстринг модуля."""
    return argparse.ArgumentParser(prog=f"mop dev {group} {verb}", add_help=False)
