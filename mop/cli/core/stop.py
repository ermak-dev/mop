"""mop stop <id> [--epoch <id> --incarnation <id> --expected-revision N] [--request-id id] [--wait seconds]

Submit the conditional scheduler stop of a puppet job: initialization, the
current session and automatic restarts stop; the clone and workspace are kept.
The stop is a single conditional scheduler write with physical readback; a lost
or uncertain reply stays unknown and is never reposted. This is not Escape for
interrupting a Claude turn and it does not delete the puppet.

With no explicit binding, first read authorized detail and use desired.revision.
The request key and absolute UTC --not-after (default two minutes) are fixed
before that query. Outstanding/unknown operations return exit 3; recover
through mop operation with the original key/epoch. --force takeover is
explicitly unsupported before I/O. MCP requires the full explicit binding.
The explicit HTTPS origin/CA and transient Django token follow mop health.
"""
from mop.cli import _control

MCP = {"annotations": "destructive",
       "actions": ["operations.stop_puppet_job", "operations.view_operation"], "args": [
    {"name": "puppet_id", "type": "string", "required": True, "help": "puppet assignment resource id"},
    {"name": "epoch", "type": "string", "flag": "--epoch", "required": True, "help": "installation epoch"},
    {"name": "incarnation", "type": "string", "flag": "--incarnation", "required": True, "help": "current execution incarnation"},
    {"name": "expected_revision", "type": "string", "flag": "--expected-revision", "required": True, "help": "desired assignment revision, not projection clock"},
    {"name": "request_id", "type": "string", "flag": "--request-id", "help": "initial command identity"},
    {"name": "not_after", "type": "string", "flag": "--not-after", "help": "absolute UTC admission deadline"},
    {"name": "wait", "type": "integer", "flag": "--wait", "help": "bounded GET wait in seconds"}]}


def main(argv):
    return _control.invoke('stop', argv)
