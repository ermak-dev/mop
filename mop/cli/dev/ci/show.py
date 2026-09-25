"""mop dev ci show <id> [--failed]: a pipeline's jobs

The pipeline's line, then one line per job: status, id, stage, name,
duration, and "(allow_failure)" for a job whose failure does not fail the
pipeline. --failed shows only the failures that do.
"""
from mop.common import gitlab
from mop.common.render import table
from mop.cli.dev.ci._common import parser

def main(argv):
    p = parser("show")
    p.add_argument("id", type=int)
    p.add_argument("--failed", action="store_true")
    a = p.parse_args(argv)
    pipe = gitlab.pipeline_by_id(a.id)
    jobs = gitlab.jobs(a.id)
    if a.failed:
        jobs = [j for j in jobs if gitlab.is_failure(j)]
    print("\n".join(table([gitlab.pipeline_line(pipe)])))
    if not jobs:
        print("no failed jobs" if a.failed else "no jobs")
        return 0
    print()
    print("\n".join(table([gitlab.job_line(j) for j in jobs])))
    return 0
