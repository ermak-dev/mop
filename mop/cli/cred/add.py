"""add a provider key as a credential: mop cred add <name> --profile P --key-file F|- [--owner email]

The key is read from the file (or stdin with -), never from an argument:
a key in the command line lands in shell history and `ps`. It travels to
the server once, over the bus, and never comes back: `mop cred list`
shows the record without it. Silent on success.
"""
import sys

from mop.cli import lib
from mop.common import bus


def main(argv):
    name = profile = key_file = None
    owner = ""
    rest = list(argv)
    while rest:
        a = rest.pop(0)
        if a == "--profile" and rest:
            profile = rest.pop(0)
        elif a == "--key-file" and rest:
            key_file = rest.pop(0)
        elif a == "--owner" and rest:
            owner = rest.pop(0)
        elif a.startswith("-") and a != "-" or name is not None:
            lib.usage(__doc__)
        else:
            name = a
    if not (name and profile and key_file):
        lib.usage(__doc__)
    if key_file == "-":
        key = sys.stdin.read()
    else:
        try:
            with open(key_file, encoding="utf-8") as f:
                key = f.read()
        except OSError as e:
            sys.exit(f"--key-file {key_file}: {e.strerror or e}")
    bus.call_cluster("cred_add", project=bus.ADMIN, name=name, profile=profile,
                     key=key.strip(), owner=owner)
    return 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
