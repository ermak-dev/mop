"""mop server credentials --bootstrap PATH --socket PATH --key-id ID [--peer-uid UID ...]: isolated credential service

Runs only the fresh-install credential process, never deploys or provisions it.
Bootstrap is an explicit protected file with role credential, its restricted DB
principal, Django signing trust and encryption-root reference. No secret values
are accepted in argv. The socket parent must already be service-owned and have
no other writers. Existing endpoints are refused, never replaced automatically.

  --bootstrap PATH   absolute private controller bootstrap file
  --socket PATH      absolute Unix socket in a protected service-owned directory
  --key-id ID        retained root key identifier (must remain stable across restarts)
  --peer-uid UID     allowed local frontend UID, repeatable; omitted means deny all
  --socket-group GID optional service group for socket 0660; otherwise mode 0600

SO_PEERCRED and current standard Access token/session credentials are both
required. Web, worker and gateway do not receive the root. There is no decrypt
RPC, generic model editing, network listener, schema maintenance or automatic replay.
Missing real assignment/approval composition refuses issuance, not permission-only
success. Service identities, DB grants and units are provisioned separately.
"""
import argparse
import os


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bootstrap', required=True)
    parser.add_argument('--socket', required=True)
    parser.add_argument('--key-id', required=True)
    parser.add_argument('--peer-uid', action='append', type=int, default=[])
    parser.add_argument('--socket-group', type=int)
    args = parser.parse_args(argv)
    os.environ['MOP_CONTROLLER_BOOTSTRAP'] = args.bootstrap
    from mop.server.controller.runtime import configure
    configure()
    from mop.server.controller.credentials import serve
    return serve(path=args.socket, allowed_uids=args.peer_uid,
                 key_id=args.key_id, socket_group=args.socket_group)
