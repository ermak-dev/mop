"""mop node up <id> --epoch <id> --expected-revision N [--request-id id] [--wait seconds]

Submit the published durable enable action, not the old cluster up RPC.
Requires an explicit node revision and installation epoch. --not-after supplies
a UTC admission deadline. The receipt is printed as a versioned operation;
accepted/running/unknown are not success. Recover with mop operation, using GET
and the original request-id/epoch rather than repeating a submitted effect.
The explicit HTTPS origin/CA and transient Django token follow mop health.
"""
from mop.cli import _control

MCP = {"annotations": "destructive", "human_only": True, "actions": ["catalog.enable_node", "operations.view_operation"], "args": [
    {"name": "node_id", "type": "string", "required": True, "help": "node resource id"},
    {"name": "epoch", "type": "string", "flag": "--epoch", "required": True, "help": "installation epoch"},
    {"name": "expected_revision", "type": "string", "flag": "--expected-revision", "required": True, "help": "node CAS revision"},
    {"name": "request_id", "type": "string", "flag": "--request-id", "help": "initial command identity"},
    {"name": "not_after", "type": "string", "flag": "--not-after", "help": "UTC admission deadline"},
    {"name": "wait", "type": "integer", "flag": "--wait", "help": "bounded GET wait in seconds"}]}


def main(argv):
    return _control.invoke('node up', argv)
