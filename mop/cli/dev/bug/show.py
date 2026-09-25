"""mop dev bug show <iid>: the full body of an issue
"""
from mop import gitlab
from mop.cli.dev.bug._common import parser


def main(argv):
    p = parser("show")
    p.add_argument("iid")
    a = p.parse_args(argv)
    i = gitlab.issue(a.iid)
    print(f"#{i['iid']} {i['title']}")
    print(f"{i['state']} · {', '.join(i['labels']) or 'no labels'} · {i['web_url']}\n")
    print(i.get("description") or "(empty)")
