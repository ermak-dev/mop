"""pool projects: mop project [add <git-origin>|delete <name>|list]

  mop project              what the pool serves: one line per project
  mop project add [origin] register a project for the pool
                           (default: this working copy's origin)
  mop project delete <name> take it off the pool
  mop project limit [<name> <N|none>]  puppet limit of a project
  mop project list [--origins]  the same list, for scripts

A project is one repository and one slice of the pool; its name is the
basename of its origin, and puppet names are built from it.
"""
from mop.cli import lib
from mop.cli.project import list as _list


def main(argv):
    if argv:
        lib.usage(__doc__)
    return _list.main([])
