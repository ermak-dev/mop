"""Общее глаголам CI: разбор аргументов."""
import functools

from mop.cli.dev import _common as dev_common

# Одна фабрика на обе группы (#263): prog -- `mop dev ci <глагол>`.
parser = functools.partial(dev_common.parser, "ci")
