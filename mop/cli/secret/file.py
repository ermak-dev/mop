"""secret files of the project: mop secret file add|list|remove

  mop secret file add <path>...     a file of this working copy; on the puppets
                                    it lands at the same path in the clone
  mop secret file list
  mop secret file remove <name>...
"""
import base64
import os

from mop.cli import lib
from mop.cli.secret import _common


def main(argv):
    if not argv or argv[0] not in ("add", "list", "remove"):
        lib.usage(__doc__)
    verb, names = argv[0], argv[1:]
    if (verb == "list") != (not names):
        lib.usage(__doc__)
    project = _common.project(__doc__)
    if verb == "list":
        for f in _common.ask(project, "secret_list")["files"]:
            print(f"{f['name']}\t{f['size']}")
        return 0
    if verb == "remove":
        for n in names:
            _common.ask(project, "secret_remove", kind="file", name=n)
        return 0
    top = lib.git("rev-parse", "--show-toplevel")
    for path in names:
        full = os.path.abspath(path)
        name = os.path.relpath(full, top)
        if name.startswith(".."):
            lib.usage(f"{path}: outside this working copy ({top})")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError as e:
            lib.usage(f"{path}: {e.strerror}")
        _common.ask(project, "secret_put", kind="file", name=name,
                    data=base64.b64encode(data).decode())
    return 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
