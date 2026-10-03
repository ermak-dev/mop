"""mop dev bug edit <iid> [--title T] [--body TEXT|--body-file F|-] [--epic N | --no-epic]: change an issue

Replaces the title and/or the whole body; the body comes from --body,
--body-file or stdin (-). The epic marker on the first line survives a body edit
unless --epic names another parent or --no-epic removes it. Nothing named:
usage, not a silent no-op.
"""
from mop.common import gitlab
from mop.cli import lib
from mop.cli.dev.bug._common import parser, read_body

MCP = {"annotations": "destructive", "args": [
    {"name": "iid", "type": "string", "required": True},
    {"name": "title", "type": "string", "flag": "--title"},
    {"name": "body", "type": "string", "flag": "--body"},
    {"name": "epic", "type": "string", "flag": "--epic"},
    {"name": "no_epic", "type": "boolean", "flag": "--no-epic"}]}


def main(argv):
    p = parser("edit")
    p.add_argument("iid")
    p.add_argument("--title")
    body = p.add_mutually_exclusive_group()
    body.add_argument("--body")
    body.add_argument("--body-file")
    p.add_argument("--epic")
    p.add_argument("--no-epic", action="store_true")
    a = p.parse_args(argv)
    if a.epic and a.no_epic:
        lib.usage(__doc__)
    body = None
    if a.body is not None or a.body_file:
        old = gitlab.issue(a.iid).get("description") or ""
        epic = None if a.no_epic else (a.epic if a.epic else gitlab.KEEP)
        text = a.body.strip() if a.body is not None else read_body(None, a.body_file)
        body = gitlab.replace_body(old, text, epic)
    elif a.epic or a.no_epic:
        # Родство без нового тела: то же тело, другой (или никакой) маркер.
        old = gitlab.issue(a.iid).get("description") or ""
        body = gitlab.replace_body(old, old, None if a.no_epic else a.epic)
    try:
        gitlab.edit(a.iid, a.title, body)
    except ValueError as e:
        lib.usage(__doc__ + f"\n{e}")
    print(f"#{a.iid} edited")
