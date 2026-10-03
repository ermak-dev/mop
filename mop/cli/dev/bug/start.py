"""mop dev bug start <iid> [slug] [--no-branch] [--worker W]: claim an issue — status::wip plus a branch
"""
from mop.cli import lib
from mop.common import gitlab
from mop.cli.dev.bug._common import parser

# Обязательный true не позволяет MCP переключить общий checkout мастера.
MCP = {"annotations": "destructive", "args": [
    {"name": "iid", "type": "string", "required": True},
    {"name": "slug", "type": "string"},
    {"name": "worker", "type": "string", "flag": "--worker"},
    {"name": "no_branch", "type": "boolean", "flag": "--no-branch", "required": True,
     "help": "Required: leave the master checkout untouched"}]}


def main(argv):
    p = parser("start")
    p.add_argument("iid")
    p.add_argument("slug", nargs="?")
    p.add_argument("--worker")
    p.add_argument("--no-branch", action="store_true")
    a = p.parse_args(argv)
    """Взять задачу: метка wip плюс ветка.

    Ветку заводит исполнитель в своём клоне, поэтому у мастера есть
    --no-branch: общий чекаут мастера не переключается никогда."""
    i = gitlab.issue(a.iid)
    branch = gitlab.branch_name(i["labels"], a.iid, a.slug or i["title"])
    gitlab.relabel(a.iid, gitlab.with_status(i["labels"], "wip"))
    note = f"Работа начата в ветке `{branch}`"
    if a.worker:
        note += f" · воркер {a.worker}"
    gitlab.comment(a.iid, note + ".")
    if a.no_branch:
        print(f"#{a.iid} claimed; branch for the executor: {branch}")
        return
    lib.git("fetch", "origin")
    lib.git("checkout", "-b", branch, lib.default_branch())
    print(f"#{a.iid} claimed, on {branch}")
