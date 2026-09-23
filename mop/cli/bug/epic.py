"""mop bug epic <iid>: the epic's children and progress
"""
from mop import gitlab
from mop.render import table
from mop.cli.bug._common import parser


def main(argv):
    p = parser("epic")
    p.add_argument("iid")
    a = p.parse_args(argv)
    """Дети эпика и прогресс. Закрытых считаем по состоянию задачи, а не по
    метке: закрыть можно и из веба, и тогда метка отстанет от правды."""
    kids = gitlab.children(a.iid)
    if not kids:
        print(f"#{a.iid}: no children yet")
        return
    done = sum(1 for i in kids if i["state"] == "closed")
    print("\n".join(table(
        [(f"#{i['iid']}",
          "closed" if i["state"] == "closed" else
          (gitlab.scoped(i["labels"], "status::").removeprefix("status::") or "-"),
          i["title"]) for i in kids])))
    print(f"\n{done}/{len(kids)} closed")
