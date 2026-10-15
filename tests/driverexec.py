"""Немодельная диагностика тела через штатный драйвер (#646).

HYPOTHESIS: отсутствует тонкий CLI для driver.argv, и живая проверка зависит от UI Claude.
SOLUTION: операторская узловая команда передаёт точный argv без shell.
STATUS: FIXED — see #646.
"""
import asyncio
import importlib
import shlex
import tempfile
from pathlib import Path
from types import SimpleNamespace

import hermetic
from _lib import Checks, patched, run_command
from mop import driver
from mop.common import config


def main():
    c = Checks()
    try:
        command = importlib.import_module("mop.cli.driver.exec")
    except ImportError:
        c.check("#646 the driver has a non-model exec command", False)
        return c.report("driverexec")
    name = "pu-example-1"
    seen = []
    async def bodies():
        return [name]
    current = SimpleNamespace(bodies=bodies, argv=lambda n: ["body-exec", n, "--"])
    with tempfile.TemporaryDirectory(prefix="mop-driverexec-646-") as root:
        node = Path(root, "node.env")
        node.write_text("MOP_DRIVER=fixture\n")
        with patched(config, NODE_ENV_FILE=str(node)), \
             patched(driver, current=lambda: current), \
             patched(command.subprocess, call=lambda argv, **kw: seen.append(argv) or 0):
            out, err, code = run_command(command.main,
                    [name, "--", "printf", "%s", "literal; not shell"], via_cli=True)
            c.check("#646 SSH receives one quoted command whose arguments stay literal",
                    code == 0 and len(seen) == 1
                    and seen[0][:3] == ["body-exec", name, "--"]
                    and len(seen[0]) == 4
                    and shlex.split(seen[0][-1]) == ["printf", "%s", "literal; not shell"],
                    (code, out, err, seen))
            for argv in ([name], ["../bad", "--", "true"], [name, "--"],
                         ["pu-other-2", "--", "true"], [name, "true"]):
                before = list(seen)
                out, err, code = run_command(command.main, argv, via_cli=True)
                c.check("#646 malformed or unknown targets do not execute",
                        code != 0 and seen == before, (argv, code, err))
        with patched(config, NODE_ENV_FILE=str(Path(root, "absent"))), \
             patched(command.subprocess, call=lambda *a, **k: seen.append("bad") or 0):
            out, err, code = run_command(command.main, [name, "--", "true"], via_cli=True)
            c.check("#646 a controller or client cannot execute node bodies",
                    code != 0 and "node" in err and "bad" not in seen, (code, err))
    return c.report("driverexec")


if __name__ == "__main__":
    raise SystemExit(main())
