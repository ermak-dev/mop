"""mop server callout: the bus's auth callout (unit mop-callout)

On the server, as the pool user. The bus asks it at every connect:
people are checked by the identity provider and keep their rights,
only over WebSocket; puppets are checked against the server's own
password files. docs/BUS.md.
"""
import sys

from mop.cli import lib


def main(argv):
    if argv:
        lib.usage(__doc__)
    try:
        from mop.server import callout
    except ImportError as e:
        sys.exit(f"the callout needs {e.name}: pip install nkeys pynacl (MOP_PIP_DEPS)")
    try:
        return lib.serve(callout.run)
    except (RuntimeError, OSError, ValueError) as e:
        sys.exit(f"callout: {e}")
