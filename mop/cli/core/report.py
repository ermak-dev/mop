"""mop report <address> <text|-> --epoch UUID [--message-id UUID] [--operation-request-id UUID] [--not-after UTC] [--priority next|now] [--notify-when-idle]: submit a private report

The opaque private address is not an assignment UUID. Message identity is fixed
before I/O and is distinct from operation correlation. A queued or accepted HTTP
submission is not native delivery. Lost submissions recover by GET only; a 404
remains unknown and never triggers a repost. A committed unknown state is
terminal for native 2.1.302 ordinary delivery: it is never re-sent, and the
message id/epoch recover it read-only. Text '-' reads bounded UTF-8 stdin.
Receiver registration, native sockets and holder headers are not public options.
"""
from mop.cli import _control

MCP = {"actions": ["execution.deliver_report"], "args": [
    {"name": "address", "type": "string", "required": True, "help": "opaque authorized private address"},
    {"name": "text", "type": "string", "required": True, "help": "private report text, at most 65536 UTF-8 bytes"},
    {"name": "epoch", "type": "string", "required": True, "flag": "--epoch", "help": "current controller epoch"},
    {"name": "message_id", "type": "string", "flag": "--message-id", "help": "stable report UUID, never an operation request id"},
    {"name": "operation_request_id", "type": "string", "flag": "--operation-request-id", "help": "optional operation correlation UUID"},
    {"name": "not_after", "type": "string", "flag": "--not-after", "help": "absolute UTC admission deadline"},
    {"name": "priority", "type": "string", "flag": "--priority", "help": "next or now"},
    {"name": "notify_when_idle", "type": "boolean", "flag": "--notify-when-idle", "help": "request separately evidenced native idle"}]}


def main(argv):
    return _control.invoke('report', argv)
