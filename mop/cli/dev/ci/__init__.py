"""pipelines, job logs, lint and runners: GitLab CI from the terminal

  mop dev ci                           the latest 15 pipelines
  mop dev ci list [-n N]               the same, N of them
  mop dev ci <id> [--failed]           a pipeline's jobs; --failed: only real failures
  mop dev ci show <id> [--failed]      the same
  mop dev ci why <id>                  why it failed: failing jobs with their log tails
  mop dev ci log <job> [--lines N] [--full]
                                   a job's log, the last 60 lines by default
  mop dev ci lint [file]               check a .gitlab-ci.yml, the working copy's by default
  mop dev ci retry <job>               run a finished job again
  mop dev ci retry --pipeline <id>     rerun a pipeline's failed jobs
  mop dev ci cancel <id>               cancel a pipeline that is still going
  mop dev ci runners                   the project's runners and what each is running

Coordinates and credentials are those of `mop dev bug`: the working copy's git
origin, GITLAB_TOKEN (or GITLAB_USER + GITLAB_PASSWORD) in .env.
"""
import importlib

from mop.cli import lib


def route(argv):
    """argv без глагола -> (глагол, argv) либо None. Пусто — список, число —
    пайплайн: самое частое — самое короткое, как у `bin/ci` в rugent.
    Глагол со своим модулем сюда не доходит: его отдаёт диспетчер."""
    if not argv:
        return "list", []
    if argv[0].isdigit():
        return "show", [*argv]
    return None


def main(argv):
    # Не lib.cluster: CI к кластеру отношения не имеет, как и трекер.
    found = route(argv)
    if found is None:
        lib.usage(__doc__)
    verb, rest = found
    return importlib.import_module(f"mop.cli.dev.ci.{verb}").main(rest)
