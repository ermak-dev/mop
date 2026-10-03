#!/usr/bin/env python3
"""Удаление папета без пула: python3 tests/delete.py."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
import hermetic  # noqa: F401,E402
from _lib import Checks, patched  # noqa: E402
sys.path.insert(0, ROOT)

from mop.common import bus, puppets  # noqa: E402


def check_alloc_driver_447(c):
    # HYPOTHESIS: после преобразования аллокации ответ с полем driver больше
    # не сохраняется, хотя выбор способа удаления всё ещё читает его из got.
    # SOLUTION: сохранить ответ alloc до преобразования аллокации и взять из него driver.
    # RESULT: оба драйвера выбирают верную ветку после снятия джоба.
    # STATUS: FIXED — see #447
    for kind, body, cleanup in (("pve", "destroyed", True),
                                ("host", "kept", False)):
        calls = []

        def cluster(verb, **fields):
            calls.append((verb, fields))
            if verb == "alloc":
                return {"alloc": {"NodeName": "hyper"}, "driver": kind}
            return {"owner_note": "taken from another master"}

        def wait(name):
            calls.append(("wait", name))

        def wipe(node, name, force):
            calls.append(("wipe", (node, name, force)))

        with patched(puppets, _cluster=cluster, _wait_stopped=wait, wipe=wipe), \
                patched(bus, login=lambda: "anton"):
            try:
                result = puppets.delete("pu-mop-1", force=True)
            except NameError as e:
                c.fail(f"#447 delete on {kind} must use allocation driver", str(e))
                continue
        c.expect(f"#447 delete on {kind} returns body and owner note", result,
                 {"node": "hyper", "body": body,
                  "owner_note": "taken from another master"})
        expected = [("alloc", {"name": "pu-mop-1"}),
                    ("delete", {"name": "pu-mop-1", "owner": "anton", "force": True})]
        if cleanup:
            expected += [("wait", "pu-mop-1"),
                         ("wipe", ("hyper", "pu-mop-1", True))]
        c.expect(f"#447 delete on {kind} cleans up only container bodies", calls, expected)


def main():
    c = Checks()
    check_alloc_driver_447(c)
    return c.report("delete")


if __name__ == "__main__":
    sys.exit(main())
