"""mop dev bug comment <iid> "<text>" | - | --body-file F: comment on an issue
"""
from mop.common import gitlab
from mop.cli.dev.bug._common import parser, read_body


def main(argv):
    p = parser("comment")
    p.add_argument("iid")
    p.add_argument("text", nargs="?")
    p.add_argument("--body-file")
    a = p.parse_args(argv)
    gitlab.comment(a.iid, read_body(a.text, a.body_file))
    print(f"#{a.iid} commented")
