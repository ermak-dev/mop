"""mop detail <id>: read one authorized puppet projection

Returns the server's versioned detail, including unknown observations and the
separate desired revision. The projection clock is not a mutation CAS revision.
A missing incarnation is unavailable, not an inferred live session or address.
No clone scan, transcript path, node RPC or local credential store is used.
The explicit HTTPS origin/CA and transient Django token follow mop health.
"""
from mop.cli import _control

MCP = {"annotations": "readonly", "actions": ["catalog.view_pool"], "args": [
    {"name": "puppet_id", "type": "string", "required": True, "help": "puppet assignment resource id"}]}


def main(argv):
    return _control.invoke('detail', argv)
