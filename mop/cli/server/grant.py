"""revoke one role principal: mop server grant revoke <principal> [--cut]

Controller-only. Selects the exact grant identifier, never a reusable puppet
name or a project-wide match. Revocation is idempotent and denies further role
operations without stopping the body or its current work.

--cut also removes this principal's stored password. Existing bus connections
retain their transport permissions until reconnecting; this is not a forced
disconnect and cannot erase credentials or secrets already delivered to a body.
"""
from mop.cli import lib
from mop.cli.server._maintenance import require as require_controller
from mop.common import soul
from mop.server import grants


def main(argv):
    if len(argv) not in (2, 3) or argv[0] != 'revoke' \
            or not soul.is_grant(argv[1]) \
            or (len(argv) == 3 and argv[2] != '--cut'):
        lib.usage(__doc__)
    require_controller()
    grants.revoke(argv[1])
    if len(argv) == 3:
        grants.cut(argv[1])
    return 0
