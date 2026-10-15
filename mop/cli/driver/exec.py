"""run a command in an existing body: mop driver exec <name> -- <command> [args...]

Operator-only, run on the node. Uses the node's driver and an existing body,
not a Claude prompt or a second session. Arguments are passed without a
shell; a nonzero child exit remains nonzero. Does not copy or display any
credentials and does not create, restart or stop the body.
"""
import asyncio
import os
import subprocess

from mop import driver
from mop.common import config


def main(argv):
    if len(argv) < 3 or argv[1] != "--":
        raise RuntimeError("use mop driver exec <name> -- <command> [args...]")
    name, command = argv[0], argv[2:]
    if not driver.valid_name(name):
        raise RuntimeError("not a puppet name")
    if not os.path.isfile(config.NODE_ENV_FILE):
        raise RuntimeError("driver exec requires the node's local configuration")
    current = driver.current()
    if name not in asyncio.run(current.bodies()):
        raise RuntimeError("the named body does not exist on this node")
    return subprocess.call([*current.argv(name), *command], stdin=subprocess.DEVNULL)
