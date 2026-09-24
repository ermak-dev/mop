"""mop ci why <id>: why a pipeline failed

Each job that failed the pipeline, with the last lines of its log. A failed
job with allow_failure is named as one that did NOT fail the pipeline. With
no failed job at all, what the pipeline itself says (yaml_errors,
failure_reason, detailed status): a rejected .gitlab-ci.yml fails with no jobs.
"""
from mop import gitlab
from mop.cli import lib
from mop.cli.ci._common import parser

# Хвост короче, чем у `log`: провалившихся джоб бывает несколько, а
# подробности одной — это уже `mop ci log`.
LINES = 30

MCP = {"annotations": "readonly", "args": [
    {"name": "id", "type": "string", "required": True, "help": "pipeline id"}]}


def main(argv):
    p = parser("why")
    p.add_argument("id", type=int)
    a = p.parse_args(argv)
    pipe = gitlab.pipeline_by_id(a.id)
    jobs = gitlab.jobs(a.id)
    tails = {j["id"]: gitlab.tail(lib.plain(gitlab.trace(j["id"])), LINES)
             for j in jobs if gitlab.is_failure(j)}
    print("\n".join(gitlab.why_lines(pipe, jobs, tails)))
    return 0
