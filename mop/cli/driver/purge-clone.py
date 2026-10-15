"""remove a deleted host puppet's data: mop driver purge-clone <name> --orphan

Run on the host node after verifying that the job was deleted on the
controller. Removes only that puppet's clone and target cache. Refuses
container drivers, live or unknown tmux sessions, unsafe paths and symlinks.
--orphan explicitly confirms that the job is no longer registered; this is
not an automatic sweep and may remove unsaved work.
"""
import os

from mop.common import config
from mop.driver import _purgeclone as purgeclone


def main(argv):
    if len(argv) != 2 or argv[1] != "--orphan":
        raise ValueError("use mop driver purge-clone <name> --orphan after deleting its job")
    if not os.path.isfile(config.NODE_ENV_FILE):
        raise RuntimeError("purge-clone requires the node's local configuration")
    gone = purgeclone.remove(argv[0], orphan=True)
    for path in gone:
        print(f"removed {path}")
    return 0
