"""mop bootstrap serve: the server's subscriber (unit mop-bootstrap)

Listens on mop.*.server.rpc and plays a project's .mop/bootstrap.yaml into a
sandbox when its node asks at start (docs/BOOTSTRAP.md).
"""
from mop import bootstrap
from mop.cli import lib


def main(_argv):
    return lib.serve(bootstrap.serve)
