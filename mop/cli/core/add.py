"""mop add <project-id> --node-id <id> --definition-id <id> --epoch <id>

Submit a durable puppet create command using an explicit project, node and
approved immutable role-definition id. The server assigns the target, name and
incarnation; no target revision is required or accepted. Origin, role-name,
shell and force inputs are not supported. No git, .env, bus or SSH fallback.

--request-id fixes the initial command identity before I/O; --not-after fixes
its absolute UTC execution deadline (default two minutes). --wait is a bounded
GET wait. An accepted receipt is not a running puppet: pending/unknown outcomes
return exit 3. Recover with mop operation using the original request key and
epoch; neither a lost reply nor a lookup failure automatically reposts create.
Actual body launch/readiness depends on the server's configured bindings.
The explicit HTTPS origin/CA and transient Django token follow mop health.
"""
from mop.cli import _control

MCP = {"annotations": "destructive", "human_only": True,
       "actions": ["execution.create_puppet", "execution.assign_work", "operations.view_operation"], "args": [
    {"name": "project_id", "type": "string", "required": True, "help": "existing project resource id"},
    {"name": "node_id", "type": "string", "flag": "--node-id", "required": True, "help": "explicit project node id"},
    {"name": "definition_id", "type": "string", "flag": "--definition-id", "required": True, "help": "approved immutable role-definition id"},
    {"name": "epoch", "type": "string", "flag": "--epoch", "required": True, "help": "installation epoch"},
    {"name": "request_id", "type": "string", "flag": "--request-id", "help": "initial command identity"},
    {"name": "not_after", "type": "string", "flag": "--not-after", "help": "absolute UTC execution deadline"},
    {"name": "wait", "type": "integer", "flag": "--wait", "help": "bounded GET wait in seconds"}]}


def main(argv):
    return _control.invoke('add', argv)
