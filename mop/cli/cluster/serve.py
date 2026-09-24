"""mop cluster serve: the server's subscriber (unit mop-cluster)

Listens on mop.*.cluster.rpc and answers the pool's verbs over Nomad —
the roster, a puppet's life cycle, the nodes. The job spec is built here,
never accepted from the asker.
"""
from mop import cluster
from mop.cli import lib


def main(_argv):
    return lib.serve(cluster.serve)
