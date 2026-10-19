"""mop dispatch <id> <text|-> [--epoch <id> --incarnation <id> --expected-revision N] [--request-id id] [--wait seconds]

Submit one task to an explicit puppet assignment UUID through /api/v1/dispatches.
Dispatch is a separate server-authorized operation, not a send alias: the server
checks dispatch permission, assignment ownership and the recipient's approved
receiving policy. This command does not select a free target or take over work.

Without a full binding, authorized detail supplies the epoch, incarnation and
desired revision. The request key and UTC --not-after (default two minutes) are
fixed before context/detail I/O. MCP requires the complete explicit binding.
Text '-' reads bounded stdin, at most 16384 UTF-8 bytes, nonempty and without NUL.
--priority now|next|later defaults to next; --wait-seconds N (0..60) is a server
command field, not evidence of idle state. --wait seconds observes the operation
with bounded GET requests. accepted/unknown/outstanding operations exit 3, not
delivered. A lost response recovers by the same request-id and epoch, never
resubmission. Local names/sockets, force, reply-to and idle notifications are unsupported.
There is no NATS, SSH, local state or credential-store fallback. The HTTPS
origin/CA and transient Django token follow mop health.
"""
from mop.cli import _control

MCP = {"annotations": "destructive",
       "actions": ["execution.dispatch_task", "operations.view_operation"], "args": [
    {"name": "puppet_id", "type": "string", "required": True, "help": "explicit puppet assignment resource id"},
    {"name": "message", "type": "string", "required": True, "help": "private UTF-8 task, at most 16384 bytes"},
    {"name": "epoch", "type": "string", "flag": "--epoch", "required": True, "help": "installation epoch"},
    {"name": "incarnation", "type": "string", "flag": "--incarnation", "required": True, "help": "current execution incarnation"},
    {"name": "expected_revision", "type": "string", "flag": "--expected-revision", "required": True, "help": "desired assignment revision"},
    {"name": "priority", "type": "string", "flag": "--priority", "help": "now, next or later; default next"},
    {"name": "wait_seconds", "type": "integer", "flag": "--wait-seconds", "help": "server command field, 0..60; not idle confirmation"},
    {"name": "request_id", "type": "string", "flag": "--request-id", "help": "initial command identity"},
    {"name": "not_after", "type": "string", "flag": "--not-after", "help": "absolute UTC admission deadline"},
    {"name": "wait", "type": "integer", "flag": "--wait", "help": "bounded GET-only operation wait in seconds"}]}


def main(argv):
    return _control.invoke('dispatch', argv)
