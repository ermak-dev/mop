"""mop server callout: the bus's auth callout (unit mop-callout)

On the server, as the pool user. nats-server asks it at every connect of
someone not in auth_users: people are checked by the identity provider and
get the rights they had in users.conf, only over WebSocket; puppets are
checked against the server's own password files. docs/BUS.md.
"""
import sys

from mop.cli import lib


def main(argv):
    if argv:
        lib.usage(__doc__)
    try:
        from mop import callout
    except ImportError as e:
        sys.exit(f"mop server callout needs {e.name}: pip install nkeys pynacl (MOP_PIP_DEPS)")
    try:
        return lib.serve(callout.run)
    except (RuntimeError, OSError, ValueError) as e:
        sys.exit(f"mop server callout: {e}")
