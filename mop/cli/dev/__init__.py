"""the developer's own commands: the project's tracker, CI and checks, not the pool

  mop dev bug   project tracker: GitLab issues, one issue per unit of work
  mop dev ci    pipelines, job logs, lint and runners: GitLab CI from the terminal
  mop dev docs  regenerate docs/CLI.md, the CLI reference, from the docstrings
  mop dev test  run the checks in tests/, all or the named ones
  mop dev web   build the dashboard (web/dist) and check the committed build

Run from a working copy of the project: the coordinates come from its git
origin, the credentials from .env. Not in MCP: the master runs them from
its shell.
"""


def main(argv):
    # lib -- здесь, а не на уровне модуля (#302): пакет импортирует и
    # `mop dev web build`, которому библиотека шины не нужна, а lib её тянет.
    from mop.cli import lib
    lib.usage(__doc__)
