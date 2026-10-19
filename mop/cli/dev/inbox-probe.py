"""mop dev inbox-probe --output new-dir [--native-binary PATH] [--timeout 1..30]: owned native private-inbox fixture

Builds the existing isolated normal nonroot Docker fixture with a finite source
closure and the declared aiohttp dependency. Runtime has no network except its
own loopback HTTPS/WSS and dummy Messages provider. No host config, credentials,
mounts, pool, model settings or native startup composition are used or changed.
Requires the installed native 2.1.302. The real LocalBridge must observe matched
native acceptance and idle; absence remains unknown. The controlled WSS receipt
is not PostgreSQL commit proof. Real PostgreSQL integration is a separate check.
The report records source hashes, actual version, kernel isolation and cleanup.
"""
from pathlib import Path
import argparse

from mop.client import chatprobe


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, choices=range(1, 31), default=20)
    parser.add_argument('--native-binary', type=Path)
    args = parser.parse_args(argv)
    from mop.client import chatprobe_isolation
    report = chatprobe_isolation.run(args.output, args.timeout, chatprobe.managed_paths(),
        private_inbox=True, native_binary=args.native_binary)
    print(report['report_path'])
    print('fixture-backed; paid provider not tested')
    for name, value in report['scenarios'].items():
        print(name+': '+value['outcome'])
    for error in report['errors']:
        print('runner error: '+error)
    return 1 if report['errors'] else 0
