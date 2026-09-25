"""mop dev bug relabel <iid> [--status S] [--label L ...]: change labels, reopen when the status is an open one
"""
from mop.common import gitlab
from mop.cli.dev.bug._common import parser


def main(argv):
    p = parser("relabel")
    p.add_argument("iid")
    p.add_argument("--status", choices=gitlab.OPEN_STATUSES + gitlab.CLOSE_STATUSES)
    p.add_argument("--label", action="append", default=[])
    a = p.parse_args(argv)
    i = gitlab.issue(a.iid)
    labels = gitlab.with_status(i["labels"], a.status) if a.status else list(i["labels"])
    for l in a.label:
        group = gitlab.check_label(l).split("::")[0]
        labels = [x for x in labels if not x.startswith(f"{group}::")] + [l]
    (gitlab.reopen if gitlab.relabel_action(i["state"], a.status) == "reopen"
     else gitlab.relabel)(a.iid, labels)
    print(f"#{a.iid}: {', '.join(labels)}")
