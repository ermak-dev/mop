"""project secrets: mop secret file|var add|list|remove

  mop secret file add <path>...     files of this working copy, kept on the server
  mop secret file list              what the project has: name and size
  mop secret file remove <name>...
  mop secret var add VAR=VALUE...   variables for the puppets' sessions
  mop secret var list               names only: a list is no way to read a secret
  mop secret var remove VAR...

The project is this working copy's, the server its binding (mop join). The
server keeps them and every puppet of the project gets them at each
start: files in the root of its clone, variables in its session. A puppet
already running gets a change at its next start (mop recycle).
"""
from mop.cli import lib


def main(argv):
    lib.usage(__doc__)
