"""Общее у `mop secret file` и `mop secret var`: проект рабочей копии и глагол."""
import sys

from mop.cli import lib
from mop import bus, puppets


def project(doc):
    """Проект этой рабочей копии: секреты -- проекта, а назван он origin'ом."""
    return puppets.project_of(lib.origin(None, doc))


def ask(project, verb, **fields):
    """Глагол сервиса на субъекте проекта. Отказ -- сообщение и выход."""
    got = bus.ask_cluster(verb, project=project, **fields)
    if got.get("error"):
        lib.fail(got["error"])
        sys.exit(1)
    return got
