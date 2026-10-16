"""mop node drain <id> --epoch <id> --expected-revision N [--request-id id] [--wait seconds]

Submit the controller's durable drain action with an explicit node binding.
The server owns eligibility, scope, resource claims and relocation. accepted
is not completed: exit 3 retains an outstanding/unknown operation, exit 0 means
observed succeeded. --not-after supplies an explicit UTC admission deadline.
Recover a lost response with mop operation --request-id <id> --epoch <id>;
recovery and wait use GET, never a new effect key or an automatic resubmission.
The explicit HTTPS origin/CA and transient Django token follow mop health.
"""
from mop.cli import _control

MCP = {"annotations": "destructive", "human_only": True, "actions": ["catalog.drain_node", "operations.view_operation"], "args": [
    {"name": "node_id", "type": "string", "required": True, "help": "node resource id"},
    {"name": "epoch", "type": "string", "flag": "--epoch", "required": True, "help": "installation epoch"},
    {"name": "expected_revision", "type": "string", "flag": "--expected-revision", "required": True, "help": "node CAS revision"},
    {"name": "request_id", "type": "string", "flag": "--request-id", "help": "initial command identity"},
    {"name": "not_after", "type": "string", "flag": "--not-after", "help": "UTC admission deadline"},
    {"name": "wait", "type": "integer", "flag": "--wait", "help": "bounded GET wait in seconds"}]}


def main(argv):
    return _control.invoke('node drain', argv)
