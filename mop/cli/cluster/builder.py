"""mop cluster builder: the server's image builder (unit mop-builder)

Listens on mop.admin.build.rpc and builds a project's image on the
hypervisors when an operator asks, streaming the build's steps back. Runs as
the controller's user: the playbook needs its inventory and ssh keys.
"""
import asyncio

from mop import builder


def main(_argv):
    try:
        asyncio.run(builder.serve())
    except KeyboardInterrupt:
        return 0
    return 0
