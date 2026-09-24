"""cluster service: mop cluster [serve|check|users|reload|builder] — Nomad behind the bus

  mop cluster check    is the service answering, and does it see Nomad
  mop cluster serve    the server's subscriber (unit mop-cluster)
  mop cluster users    write the bus users file from the server's registry
  mop cluster reload   make nats reread its config, and check it took
  mop cluster builder  the server's image builder (unit mop-builder)

The only thing on the installation that talks to Nomad on someone else's
behalf. A master and an operator ask a verb on mop.<project>.cluster.rpc;
the service checks whether they may and only then calls the API, so the
management token stays on the server (#80, mop/cluster.py).
"""
from mop.cli import lib
from mop.cli.cluster import check as _check


def main(argv):
    if argv:
        lib.usage(__doc__)
    return _check.main([])
