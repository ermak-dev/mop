"""mop ci runners: the project's runners and the jobs they are running

A runner per line: ON/OFF (takes work or paused), id, description, status
(whether GitLab sees it), tags. Then each running job with the runner that
holds it.
"""
from mop import gitlab
from mop.render import table

MCP = {"annotations": "readonly", "args": []}


def main(argv):
    if argv:
        raise RuntimeError("mop ci runners takes no arguments")
    got = gitlab.runners()
    print("\n".join(table([gitlab.runner_line(r) for r in got])) if got else "no runners")
    running = gitlab.running_jobs()
    print()
    print("\n".join(table([gitlab.running_line(j) for j in running]))
          if running else "no running jobs")
    return 0
