"""private live-session history and terminal control: mop session <verb>

  mop session history <name> [--cursor cursor]
  mop session terminal <name> <action> [tid] [options]

History follows the current conversation. Terminal attaches to the same tmux
session, never a second Claude process. Terminal control is an operator-only
capability: it can execute commands in the body. No automatic input replay.
"""
from mop.cli import lib


def main(argv):
    lib.usage(__doc__)
