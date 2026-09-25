"""mop server cluster builder: the server's image builder (unit mop-builder)

Listens on mop.admin.build.rpc and builds a project's image on the
hypervisors when an operator asks, streaming the build's steps back. Runs as
the controller's user: the playbook needs its inventory and ssh keys.
"""
from mop.server import builder
from mop.cli import lib


def main(_argv):
    return lib.serve(builder.serve)
