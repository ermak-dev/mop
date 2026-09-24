"""mop ci cancel <id>: cancel a pipeline that is still going

Refused for a pipeline that already finished (success, failed, canceled,
skipped), naming its state: GitLab would accept that and do nothing.
"""
from mop import gitlab
from mop.cli.ci._common import parser


def main(argv):
    p = parser("cancel")
    p.add_argument("id", type=int)
    a = p.parse_args(argv)
    refused = gitlab.pipeline_cancel_blocked(gitlab.pipeline_by_id(a.id))
    if refused:
        raise RuntimeError(refused)
    gitlab.cancel_pipeline(a.id)
    return 0
