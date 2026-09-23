"""mop cluster serve: the server's subscriber (unit mop-cluster)

Listens on mop.*.cluster.rpc and answers the pool's verbs over Nomad —
the roster, a puppet's life cycle, the nodes. The job spec is built here,
never accepted from the asker.
"""
import asyncio

from mop import cluster


def main(_argv):
    try:
        asyncio.run(cluster.serve())
    except KeyboardInterrupt:
        return 0
    return 0
