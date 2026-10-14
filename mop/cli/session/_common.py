"""Общее тонким командлетам приватной сессии: аргументы, actor и вывод."""
import argparse
import json

from mop.cli import lib
from mop.common import bus, ops, puppets


def target_context(name):
    """Факты цели через общую библиотеку, без серверных файлов у клиента."""
    spec = puppets._cluster("spec", name=name) or {}
    project = ops.target_project((spec.get("meta") or {}).get("origin"))
    if not project or project == bus.ADMIN:
        raise bus.Refused("cannot determine the target project")
    return {"node": puppets.node_of(name), "project": project}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        lib.usage("invalid session arguments")


def parser(doc):
    return Parser(add_help=False, usage=doc)


def context(name):
    lib.guard(name)
    actor = bus.login()
    if not actor:
        lib.usage("session access needs a human operator login")
    return target_context(name), actor


def show(value):
    if not isinstance(value, dict):
        value = {"error": "session returned an invalid response"}
    print(json.dumps(value, ensure_ascii=False))
    if value.get("outcome") == "unknown":
        return 2
    return 1 if value.get("error") else 0
