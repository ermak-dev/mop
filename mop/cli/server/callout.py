"""mop server callout: the bus's auth callout (unit mop-callout)

On the server, as the pool user. Handles authentication for people —
checked by the identity provider, keeping their rights, only over
WebSocket — and for puppets, checked against the server's own password
files. docs/BUS.md.
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
