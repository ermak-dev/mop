"""mop send <id> <text|-> [--epoch <id> --incarnation <id> --expected-revision N] [--request-id id] [--wait seconds]

Submit one finite remote send through /api/v1/messages. The target is a puppet
assignment resource id, never a name, pid, directory or socket. Without a full
explicit binding, authorized detail supplies the epoch, incarnation and desired
revision, not the projection clock. The request key and UTC --not-after (default
two minutes) are fixed before context/detail I/O. MCP requires the full binding.

Text '-' reads bounded stdin; UTF-8 text is limited to 16384 bytes, nonempty and
without NUL. Newlines are preserved. Text stays in the transient request body, not
a URL, diagnostic or local store. --priority now|next|later defaults to next.
--wait-seconds N (0..60) is the server command field; --wait seconds is a bounded
GET-only operation wait. Neither option proves idle state or message delivery.

accepted/unknown/outstanding operations exit 3, not delivered. Recover with mop
operation using the original request-id and epoch; a lost reply never reposts.
Local send/--list, --mode, --force, --local, --reply-to and --notify-when-idle are
explicitly unsupported before I/O: this client has no trusted holder endpoint.
An unavailable physical message binding is a server refusal, never bus/UDS/SSH
fallback. The HTTPS origin/CA and transient Django token follow mop health.
"""
from mop.cli import _control

MCP = {"annotations": "destructive",
       "actions": ["execution.send_message", "operations.view_operation"], "args": [
    {"name": "puppet_id", "type": "string", "required": True, "help": "puppet assignment resource id"},
    {"name": "message", "type": "string", "required": True, "help": "private UTF-8 message, at most 16384 bytes"},
    {"name": "epoch", "type": "string", "flag": "--epoch", "required": True, "help": "installation epoch"},
    {"name": "incarnation", "type": "string", "flag": "--incarnation", "required": True, "help": "current execution incarnation"},
    {"name": "expected_revision", "type": "string", "flag": "--expected-revision", "required": True, "help": "desired assignment revision, not projection clock"},
    {"name": "priority", "type": "string", "flag": "--priority", "help": "now, next or later; default next"},
    {"name": "wait_seconds", "type": "integer", "flag": "--wait-seconds", "help": "server send field, 0..60; not idle confirmation"},
    {"name": "request_id", "type": "string", "flag": "--request-id", "help": "initial command identity"},
    {"name": "not_after", "type": "string", "flag": "--not-after", "help": "absolute UTC admission deadline"},
    {"name": "wait", "type": "integer", "flag": "--wait", "help": "bounded GET-only operation wait in seconds"}]}


def main(argv):
    return _control.invoke('send', argv)
