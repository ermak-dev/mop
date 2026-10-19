"""node agent: explicit private HTTPS bootstrap, one per node under systemd

  mop agent --bootstrap /absolute/private/bootstrap.json --check
  mop agent --bootstrap /absolute/private/bootstrap.json

--check performs bounded authenticated executor discovery and verifies the
installation identity, epoch and enrolled node. It never changes the executor
fence, reconciles physical scopes, polls work or claims effects. An executor-only
response without installation coordinates is refused, not upgraded locally.

Normal serve is unavailable until a complete native physical startup root is
bound. A transport export is not physical reconciliation or readiness evidence.
No .env, bus.json, ready.json or previous login fallback is supported.
"""
import argparse
import asyncio
import sys


def main(argv):
    from mop.node.bootstrap_transport import BootstrapError, load
    from mop.node.http_execution import Refused, Unavailable
    from mop.node.native_startup import check_bootstrap, serve

    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--bootstrap', required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    try:
        bootstrap = load(args.bootstrap)
        if args.check:
            asyncio.run(check_bootstrap(bootstrap))
        else:
            asyncio.run(serve(bootstrap))
    except (BootstrapError, Refused, Unavailable) as error:
        print(str(error), file=sys.stderr)
        return 1
    except ImportError:
        print('native agent HTTP runtime dependency is unavailable', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
