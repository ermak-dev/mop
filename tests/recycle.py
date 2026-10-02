#!/usr/bin/env python3
"""Рецикл папета без пула: python3 tests/recycle.py

puppets.recycle -- останов, wipe, перерегистрация; здесь -- какую ветку он
кладёт в wipe и в перерегистрацию. Шина подменена: глаголы кластера и узла
записываются, ожидание останова пропущено.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, run_command  # noqa: E402
sys.path.insert(0, ROOT)

os.environ.setdefault("MOP_SERVER_LAN", "10.0.0.1")

from mop.common import bus, context, puppets  # noqa: E402

META = {"origin": "git@example.org:g/proj.git", "llm": "claude", "branch": "master"}


def recycled(**kw):
    """Рецикл pu-proj-1 на подменённой шине. -> {глагол: поля}."""
    asked = {}

    def cluster(verb, **fields):
        asked[verb] = fields
        return {"spec": {"meta": META},
                "alloc": {"alloc": {"NodeName": "n1"}}}.get(verb, {})

    def request(node, verb, **fields):
        asked[verb] = fields
        return {}

    with patched(bus, call_cluster=cluster, request=request, login=lambda: "anton"), \
            patched(puppets, _wait_stopped=lambda name: None):
        puppets.recycle("pu-proj-1", **kw)
    return asked


def check_branch_367(c):
    # HYPOTHESIS (#367): recycle сам читал context.current().branch -- git
    # config mop.branch каталога запуска или MOP_BRANCH. `mop server gc` операторский
    # и рециклит папетов всех проектов: ветка окружения оператора перекрывала
    # ветку из меты джоба, wipe делал checkout -B на неё, перерегистрация
    # писала её в Meta. SOLUTION: ветка -- параметр recycle, None -> meta.branch;
    # контекст читает только командлет mop recycle. STATUS: FIXED — see #367
    ctx = context.Context(branch="epic/X", sources={"branch": "env"})
    with context.use(ctx):
        got = recycled()
        c.expect("gc-style recycle wipes onto meta.branch, not MOP_BRANCH",
                 got.get("wipe", {}).get("branch"), "master")
        c.expect("gc-style recycle registers meta.branch, not MOP_BRANCH",
                 got.get("update", {}).get("branch"), "master")
        try:
            got = recycled(branch="epic/Y")
        except TypeError as e:
            c.fail("recycle takes branch=", str(e))
        else:
            c.expect("explicit branch reaches wipe", got["wipe"].get("branch"), "epic/Y")
            c.expect("explicit branch reaches the registration",
                     got["update"].get("branch"), "epic/Y")

    # Командлет mop recycle -- тот, кто знает рабочую копию: он и передаёт
    # ветку контекста; mop server gc не передаёт ничего.
    from mop.cli.core import recycle as cmd
    seen = {}

    def fake(name, **kw):
        seen.update(kw)
        raise RuntimeError("stop here")

    with context.use(ctx), patched(puppets, recycle=fake):
        run_command(cmd.main, ["pu-proj-1"], via_cli=True)
    c.expect("mop recycle passes the context branch", seen.get("branch"), "epic/X")

    src = open(os.path.join(ROOT, "mop", "cli", "server", "gc.py")).read()
    c.check("mop server gc passes no branch", "puppets.recycle(name)" in src)


def main():
    c = Checks()
    check_branch_367(c)
    return c.report("recycle")


if __name__ == "__main__":
    sys.exit(main())
