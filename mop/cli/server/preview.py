"""mop server preview --directory PATH [--port 18080]: local isolated Django Admin

Starts a new disposable PostgreSQL 17 container and production Admin over
loopback HTTPS using the declared server extra of the current interpreter.
Requires local Docker and openssl. It reads no installation .env or pool data.
The directory must not exist; generated keys and the initial admin password
are written there privately, never printed. The localhost certificate is
self-signed. Username: admin. Stop with Ctrl+C (or stop this owned task).
The container and its preview database are removed on shutdown; private
bootstrap/password/log files remain. This is not a ready production pool:
unbound runtime operations still refuse and effects start disabled.
"""
import argparse
from pathlib import Path
import signal


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--port', type=int, default=18080)
    args = parser.parse_args(argv)
    from mop.server.controller.preview import PreviewPlan, running
    plan = PreviewPlan(args.directory, args.port)
    previous = signal.getsignal(signal.SIGTERM)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        with running(plan) as child:
            print(plan.url, flush=True)
            print('Username: admin', flush=True)
            print('Password file: '+str(plan.directory/'admin-password'), flush=True)
            print('Self-signed localhost certificate; preview database is disposable.', flush=True)
            return child.wait()
    except KeyboardInterrupt:
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous)
