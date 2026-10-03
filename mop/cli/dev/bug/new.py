"""mop dev bug new "<title>" [--label L ...] [--body TEXT|--body-file F] [--epic N]: open an issue

Body from --body, --body-file or stdin; --epic makes it a child of that epic. A
status label is added when none is given: status::live.
"""
from mop.common import gitlab
from mop.cli import lib
from mop.cli.dev.bug._common import parser, read_body

MCP = {"annotations": "destructive", "args": [
    {"name": "title", "type": "string", "required": True},
    {"name": "label", "type": "string", "flag": "--label", "repeatable": True},
    {"name": "body", "type": "string", "flag": "--body", "required": True},
    {"name": "epic", "type": "string", "flag": "--epic"}]}


def main(argv):
    p = parser("new")
    p.add_argument("title")
    p.add_argument("--label", action="append", default=[])
    body = p.add_mutually_exclusive_group()
    body.add_argument("--body")
    body.add_argument("--body-file")
    p.add_argument("--epic")
    a = p.parse_args(argv)
    labels = [gitlab.check_label(l) for l in a.label]
    if not gitlab.scoped(labels, "status::"):
        labels.append("status::live")
    body = a.body.strip() if a.body is not None else read_body(None, a.body_file)
    if not body:
        lib.usage(__doc__ + "\nissue body must not be empty")
    if a.epic:
        body = gitlab.with_epic(body, a.epic)
    i = gitlab.create(a.title, body, labels)
    print(f"#{i['iid']} {i['web_url']}")
