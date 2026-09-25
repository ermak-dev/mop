"""mop dev bug close <iid> [--comment "..."] [--status S]: close an issue, status::fixed by default
"""
from mop import gitlab
from mop.cli.dev.bug._common import parser


def main(argv):
    p = parser("close")
    p.add_argument("iid")
    p.add_argument("--comment")
    p.add_argument("--status", default="fixed", choices=gitlab.CLOSE_STATUSES)
    a = p.parse_args(argv)
    i = gitlab.issue(a.iid)
    if a.comment:
        gitlab.comment(a.iid, a.comment)
    gitlab.close(a.iid, gitlab.with_status(i["labels"], a.status))
    print(f"#{a.iid} closed as {a.status}")
