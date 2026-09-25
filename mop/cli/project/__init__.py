"""pool projects: mop project [add <git-origin>|delete <name>|list]

  mop project              what the pool serves: one line per project
  mop project add [origin] register a project and put it on the bus
                           (default: this working copy's origin)
  mop project delete <name> take it off the bus and out of the registry
  mop project limit [<name> <N|none>]  puppet limit of a project
  mop project list [--origins]  the same list, for scripts

A project is one repository, one slice of the pool, one master; its name is
the basename of its origin, and the pool builds puppet names from it. The
registry lives on the server and is the only answer to "which projects are
set up"; every subcommand is an operator's verb of the cluster service,
so it works from any machine with the admin role, not only from the
controller. Operators reach a project by their role in the server's identity provider.

Registering used to be a side effect of `mop server deploy <origin>` and there was
no way to take a project off. `mop server deploy` no longer takes origins.
"""
from mop.cli import lib
from mop.cli.project import list as _list


def main(argv):
    if argv:
        lib.usage(__doc__)
    return _list.main([])
