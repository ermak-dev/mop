"""Общее глаголам CI: разбор аргументов."""
import argparse


def parser(verb):
    """argparse глагола без встроенной справки: usage — докстринг модуля."""
    return argparse.ArgumentParser(prog=f"mop dev ci {verb}", add_help=False)
