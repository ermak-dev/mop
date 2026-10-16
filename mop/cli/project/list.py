"""mop project list [--cursor VALUE] [--page-size N]: query authorized projects

Returns one bounded controller page and its opaque next_cursor, without an
independent client state store or roster. Reuse the returned cursor with the same
page size. Origin and project revision come from the authorized server query.
The explicit HTTPS origin/CA and transient Django token follow mop health.
"""
from mop.cli import _control

MCP = {"annotations": "readonly", "actions": ["catalog.view_project"], "args": [
    {"name": "cursor", "type": "string", "flag": "--cursor", "help": "opaque server page cursor"},
    {"name": "page_size", "type": "integer", "flag": "--page-size", "help": "page size within 1..500"}]}


def main(argv):
    return _control.invoke('project list', argv)
