"""mop addresses <project-id> [--limit N]: query own authorized session addresses

Returns endpoint records from the controller, including registered but not live
endpoints. A Django User is not a live session. This is not a pool roster or a
remote host-session scan. Delivery addresses must come from these records.
The explicit HTTPS origin/CA and transient Django token follow mop health.
"""
from mop.cli import _control

MCP = {"annotations": "readonly", "human_only": True,
       "actions": ["sessions.view_delivery", "catalog.view_project"], "args": [
    {"name": "project_id", "type": "string", "required": True, "help": "authorized project id"},
    {"name": "limit", "type": "integer", "flag": "--limit", "help": "bounded number of own endpoint records"}]}


def main(argv):
    return _control.invoke('addresses', argv)
