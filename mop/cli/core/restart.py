"""mop restart <id> [--epoch <id> --incarnation <id> --expected-revision N] [--request-id id] [--wait seconds]

Submit the finite durable restart action, keeping the clone and branch. With no
explicit binding, first read authorized detail and use desired.revision, never
the observation/projection clock. The request key and absolute UTC --not-after
(default two minutes) are fixed before that query. A full explicit binding needs
no roster read; the server still rechecks current identity, scope and ownership.

accepted is not restarted: outstanding/unknown operations return exit 3. Recover
through mop operation with the original key/epoch; neither a lost reply nor wait
reposts the effect. --force and --fresh are explicitly unsupported before I/O.
The server must supply its physical restart binding; an unavailable API never
opens SSH or falls back to the old bus. MCP requires the full explicit binding.
The explicit HTTPS origin/CA and transient Django token follow mop health.
"""
from mop.cli import _control

MCP = {"annotations": "destructive",
       "actions": ["execution.restart_puppet", "operations.view_operation"], "args": [
    {"name": "puppet_id", "type": "string", "required": True, "help": "puppet assignment resource id"},
    {"name": "epoch", "type": "string", "flag": "--epoch", "required": True, "help": "installation epoch"},
    {"name": "incarnation", "type": "string", "flag": "--incarnation", "required": True, "help": "current execution incarnation"},
    {"name": "expected_revision", "type": "string", "flag": "--expected-revision", "required": True, "help": "desired assignment revision, not projection clock"},
    {"name": "request_id", "type": "string", "flag": "--request-id", "help": "initial command identity"},
    {"name": "not_after", "type": "string", "flag": "--not-after", "help": "absolute UTC admission deadline"},
    {"name": "wait", "type": "integer", "flag": "--wait", "help": "bounded GET wait in seconds"}]}


def main(argv):
    return _control.invoke('restart', argv)
