"""mop host-root --physical /absolute/root-owned/physical.json

Installed privileged host supervisor, not an HTTP agent or arbitrary RPC.
The installed launcher uses an isolated interpreter and immutable root-owned
release before importing this command. A user checkout is refused. The bounded
no-follow config contains only installed node/installation IDs, OS UIDs/GID and
physical paths: no credentials, .env, policy snapshots or saved executor epoch.

This root retains physical ownership across HTTP frontend disconnects. It does
not enable the incomplete native action graph or install a host service.
"""
import argparse
import asyncio
import sys


def main(argv):
    from mop.node.host_root import load, run, trusted_release
    from mop.node.host_scope import Refused, Unknown
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--physical', required=True)
    args = parser.parse_args(argv)
    try:
        trusted_release()
        config = load(args.physical)
        asyncio.run(run(config))
    except (RuntimeError, OSError, Refused, Unknown) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
