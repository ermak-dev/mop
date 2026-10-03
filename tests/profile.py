#!/usr/bin/env python3
"""Профиль мастера как инструмент MCP без запуска второго сервера (#442)."""
import os
import sys

import hermetic  # noqa: F401,E402
from _lib import Checks, patched, run_command  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import cli  # noqa: E402
from mop.common import bus, context  # noqa: E402


def main():
    c = Checks()
    path = os.path.join(cli.PACKAGE, "core", "profile.py")
    # HYPOTHESIS: проверка профиля есть только в режиме mop mcp --check,
    # который создаёт отдельный процесс вместо вызова действующего MCP.
    # SOLUTION: объявленный командлет с тем же правилом классификации.
    if not c.check("#442 a profile commandlet exists", os.path.isfile(path)):
        return c.report("profile")
    from mop.cli.core import profile
    decl = cli.declared(path)
    c.expect("#442 profile is exposed read-only", decl.get("annotations") if decl else None,
             "readonly")
    c.expect("#442 node identity", profile.profile_name(False, "cloudpub"), "node")
    c.expect("#442 operator identity", profile.profile_name(True, bus.ADMIN), "operator")
    c.expect("#442 master identity", profile.profile_name(True, "cloudpub"),
             "master of project cloudpub")
    with patched(bus, config=lambda: {"user": "anton"}, PROJECT="cloudpub"):
        with context.use(context.Context(branch="integration/test")):
            text, _, code = run_command(profile.main, [])
    c.expect("#442 command succeeds", code, 0)
    c.expect("#442 profile and effective branch", text.strip(),
             "profile master of project cloudpub; integration branch integration/test")
    with patched(bus, config=lambda: {"user": "anton"}, PROJECT="cloudpub"):
        with context.use(context.Context()):
            text, _, _ = run_command(profile.main, [])
    c.expect("#442 no branch is not origin HEAD", text.strip(),
             "profile master of project cloudpub; integration branch not set")
    # RESULT: красный на отсутствующем командлете, зелёный после реализации.
    # STATUS: FIXED — see #442
    return c.report("profile")


if __name__ == "__main__":
    sys.exit(main())
