"""mop cluster reload: make nats-server reread its config, and check it took

On the server, as the pool user. Checks the file with `nats-server -t`,
sends SIGHUP, then compares the running config's digest (/varz on the
loopback monitoring port) with the file's. A rejected reload -- nats keeps
the old config and says so only in its journal -- is a refusal with the
reason, not a silent success (#211).
"""
import sys

from mop.cli import lib
from mop import natsconf


def main(argv):
    if argv:
        lib.usage(__doc__)
    try:
        natsconf.reload()
    except RuntimeError as e:
        sys.exit(f"nats reload refused: {e}")
    return 0
