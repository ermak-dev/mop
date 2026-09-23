"""mop bootstrap serve: the server's subscriber (unit mop-bootstrap)

Listens on mop.*.server.rpc and plays a shard's .mop/bootstrap.yaml into a
sandbox when its node asks at start (docs/BOOTSTRAP.md).
"""
import asyncio

from mop import bootstrap


def main(_argv):
    try:
        asyncio.run(bootstrap.serve())
    except KeyboardInterrupt:
        return 0
    return 0
