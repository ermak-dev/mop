"""pool projects: mop project [add <git-origin>|delete <name>|list]

  mop project              what the pool serves: one line per project
  mop project add <origin> register a project and put it on the bus
  mop project delete <name> take it off the bus and out of the registry
  mop project limit [<name> <N|none>]  puppet limit of a project
  mop project list [--origins]  the same list, for scripts

A project is one repository, one slice of the pool, one master; its name is
the basename of its origin, and the pool builds puppet names from it. The
registry (~/.config/mop/projects) is the only answer to "which projects are
set up": the playbook renders the NATS user puppet-<project> from it, and
operators reach the project by their role in MOP_OPERATORS.

Registering used to be a side effect of `mop deploy <origin>` and there was
no way to take a project off (#79). `mop deploy` no longer takes origins.
"""
from mop.cli import lib
from mop.cli.project import list as _list


def main(argv):
    if argv:
        lib.usage(__doc__)
    return _list.main([])
