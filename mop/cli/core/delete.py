"""mop delete <id> [--epoch <id> --incarnation <id> --expected-revision N] [--request-id id] [--wait seconds]

Submit the durable puppet delete: the scheduler job is stopped with a
conditional write and physical readback, then purged, and the assignment is
closed for good -- no owner, no incarnation, never activated again. A container
body goes with the purged allocation; a host body keeps its clone. Delete needs
its own permission and belongs only to the human who owns the assignment;
--force takeover is explicitly unsupported before I/O.

With no explicit binding, first read authorized detail and use desired.revision.
The request key and absolute UTC --not-after (default two minutes) are fixed
before that query. Outstanding/unknown operations return exit 3; recover
through mop operation with the original key/epoch, never by reposting. MCP
requires the full explicit binding. The explicit HTTPS origin/CA and transient
Django token follow mop health.
"""
from mop.cli import _control

MCP = {"annotations": "destructive",
       "actions": ["execution.delete_puppet", "operations.view_operation"], "args": [
    {"name": "puppet_id", "type": "string", "required": True, "help": "puppet assignment resource id"},
    {"name": "epoch", "type": "string", "flag": "--epoch", "required": True, "help": "installation epoch"},
    {"name": "incarnation", "type": "string", "flag": "--incarnation", "required": True, "help": "current execution incarnation"},
    {"name": "expected_revision", "type": "string", "flag": "--expected-revision", "required": True, "help": "desired assignment revision, not projection clock"},
    {"name": "request_id", "type": "string", "flag": "--request-id", "help": "initial command identity"},
    {"name": "not_after", "type": "string", "flag": "--not-after", "help": "absolute UTC admission deadline"},
    {"name": "wait", "type": "integer", "flag": "--wait", "help": "bounded GET wait in seconds"}]}


def main(argv):
    return _control.invoke('delete', argv)
