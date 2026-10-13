"""stop a puppet job: mop stop <name>

Operator only. Stops initialization, the current session and automatic
restarts. The container, clone and bootstrap workspace are kept.
Use mop update <name> to start the job again. This is not Escape for
interrupting a Claude turn, and it does not delete the puppet.
"""
from mop.cli import lib
from mop.common import bus, ops


MCP = {"annotations": "destructive", "args": [
    {"name": "name", "type": "string", "required": True,
     "help": "puppet name, pu-<project>-<n>"}]}


def main(argv):
    if len(argv) != 1 or argv[0].startswith("-"):
        lib.usage(__doc__)
    name = argv[0]
    def transport(verb, **fields):
        return bus.call_cluster(verb, project=bus.ADMIN, **fields)
    verdict = ops.stop(transport, name, login=bus.login())
    if verdict["outcome"] == ops.REFUSED:
        raise bus.Refused(verdict["error"])
    if verdict["outcome"] != ops.REGISTERED:
        lib.fail(verdict["error"])
        return 1
    return 0


main = lib.cluster(main)
