"""open a terminal on a puppet: mop attach <name>

Reconnects to a running puppet and creates nothing: a new one is
`mop add` or `mop <role>`. Detaching (or a dropped connection) leaves the
puppet running. Goes over ssh, not the bus: a live terminal is not
something a bus verb hands over.
"""
import os

from mop.cli import lib
from mop.cli.core import _common


def main(argv):
    if len(argv) != 1:
        lib.usage(__doc__)
    name = argv[0]
    lib.guard(name)
    argv = _common.attach_argv(name)
    os.execvp(argv[0], argv)



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
