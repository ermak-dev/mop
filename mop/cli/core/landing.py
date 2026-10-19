"""mop landing <show|take|give|recover-take|recover-give> <project-id> [--epoch id --expected-revision N] [--owner-id N]

Show one authorized project landing resource over HTTPS. An absent token is
explicitly unmaterialized at revision 0, not held and not silently created.
Take and give require an explicit canonical project id, installation epoch
and exact decimal-string revision. Ownership is the actual current human Actor,
not a puppet name, delegated owner, git identity or local project context.
Give releases only that human's own token. There is no force takeover.

A successful SQL CAS response is printed only after the server commit. A lost
or malformed response triggers only a GET of the same project/epoch. Recovery
compares the exact successor revision and human owner (or null after give).
It is a current observation, not a historical command receipt; even a matching
observation returns exit 3 with command_outcome unknown. Never retake blindly.
For a later read-only recovery, recover-take/recover-give also require the
original actual Actor's --owner-id. A later revision, even for that same owner,
does not match. No Operation/request-id or node anchor is created.

The explicit HTTPS origin/CA and transient Django token follow mop health;
no .env, legacy server context, bus or local login store is used.
MCP conservatively requires all three canonical permissions and a human actor;
the server checks the individual read/acquire/release permission on each call.
"""
from mop.cli import _control

MCP = {"annotations": "destructive", "human_only": True,
       "actions": ["execution.view_landing", "execution.acquire_landing", "execution.release_landing"],
       "args": [
    {"name": "action", "type": "string", "required": True,
     "help": "show, take, give, recover-take or recover-give"},
    {"name": "project_id", "type": "string", "required": True, "help": "canonical project resource id"},
    {"name": "epoch", "type": "string", "flag": "--epoch", "help": "installation epoch; required except for show"},
    {"name": "expected_revision", "type": "string", "flag": "--expected-revision",
     "help": "exact CAS revision; required except for show; 0 means unmaterialized"},
    {"name": "owner_id", "type": "integer", "flag": "--owner-id",
     "help": "original actual human Actor id; required only for read-only recovery"}]}


def main(argv):
    return _control.invoke('landing', argv)
