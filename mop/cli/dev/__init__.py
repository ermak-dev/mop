"""the developer's own commands: the project's tracker, CI and checks, not the pool

  mop dev bug   project tracker: GitLab issues, one issue per unit of work
  mop dev ci    pipelines, job logs, lint and runners: GitLab CI from the terminal
  mop dev test  run the checks in tests/, all or the named ones

Run from a working copy of the project: the coordinates come from its git
origin, the credentials from .env. Not in MCP: the master runs them from
its shell.
"""
from mop.cli import lib


def main(argv):
    lib.usage(__doc__)
