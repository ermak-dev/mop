"""the developer's own commands: the project's tracker and CI, not the pool

  mop dev bug   project tracker: GitLab issues, one issue per unit of work
  mop dev ci    pipelines, job logs, lint and runners: GitLab CI from the terminal

Run from a working copy of the project: the coordinates come from its git
origin, the credentials from .env. Not in MCP: the master runs them from
its shell.
"""
from mop.cli import lib


def main(argv):
    lib.usage(__doc__)
