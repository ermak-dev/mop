"""project tracker: GitLab issues, one issue per unit of work

  mop bug list [--all] [--label L] [-t TEXT]   open issues, worst first
  mop bug show <iid>                           full body
  mop bug new "Заголовок" [--label L ...]      body from --body-file or stdin
  mop bug new "Заголовок" --epic 12            child of epic #12
  mop bug epic <iid>                           the epic's children and progress
  mop bug comment <iid> "текст"                comment (or - for stdin)
  mop bug start <iid> [slug] [--no-branch]     claim: status::wip + branch
  mop bug close <iid> --comment "..."          close, default status::fixed
  mop bug relabel <iid> --status live          back to the queue
  mop bug relabel <iid> --status parked        postponed: stays open, not worked on
  mop bug labels                               the label vocabulary

Coordinates come from the working copy's git origin; credentials are
GITLAB_TOKEN (or GITLAB_USER + GITLAB_PASSWORD) in .env. Titles, bodies and comments are in
Russian; branch names and commits stay English.
"""
from mop.cli import lib


def main(argv):
    # Не lib.cluster: трекер к кластеру отношения не имеет, и требовать
    # настроенный Nomad ради чтения задачи не за что.
    lib.usage(__doc__)
