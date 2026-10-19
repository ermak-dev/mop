"""mop project limit <project-id> <N|none> [--expected-revision N]

Change only the desired puppet limit of an existing project through its Django
catalogue configuration endpoint. The target is a canonical project UUID, not a
project name or local checkout. Zero blocks new creation; none lifts the limit.
Existing running puppets are not stopped, rebuilt, moved or redeployed.

An authorized project read supplies the exact unchanged origin and current CAS
revision. --expected-revision pins the revision explicitly: a different read
revision refuses without mutation, never replaces the submitted expectation.
MCP requires the explicit revision. Ordinary Django project view/change/limit
permissions and current project scope are enforced by the server.

One configuration request is sent. This catalogue CAS has no operation request
key. A lost response or uncertain commit exits 3 as unknown; it never repeats the
mutation or declares a later matching observation a historical success receipt.
Known conflicts refuse. Use mop project to observe the desired catalogue. No
bus, SSH, local state, .env or credential-store fallback. HTTPS origin/CA and the
transient Django token follow mop health.
"""
from mop.cli import _control

MCP = {"annotations": "destructive",
       "actions": ["catalog.view_project", "catalog.change_project", "catalog.set_project_limit"], "args": [
    {"name": "project_id", "type": "string", "required": True, "help": "existing project resource UUID"},
    {"name": "limit", "type": "string", "required": True, "help": "canonical integer 0..2147483647, or none"},
    {"name": "expected_revision", "type": "string", "flag": "--expected-revision", "required": True,
     "help": "exact desired project revision"}]}


def main(argv):
    return _control.invoke('project limit', argv)
