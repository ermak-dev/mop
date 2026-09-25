"""mop dev bug new "Заголовок" [--label L ...] [--body-file F] [--epic N]: open an issue

Body from --body-file or stdin; --epic makes it a child of that epic. A
status label is added when none is given: status::live.
"""
from mop.common import gitlab
from mop.cli.dev.bug._common import parser, read_body


def main(argv):
    p = parser("new")
    p.add_argument("title")
    p.add_argument("--label", action="append", default=[])
    p.add_argument("--body-file")
    p.add_argument("--epic")
    a = p.parse_args(argv)
    labels = [gitlab.check_label(l) for l in a.label]
    if not gitlab.scoped(labels, "status::"):
        labels.append("status::live")
    body = read_body(None, a.body_file)
    if a.epic:
        body = gitlab.with_epic(body, a.epic)
    i = gitlab.create(a.title, body, labels)
    print(f"#{i['iid']} {i['web_url']}")
