"""mop dev web test [file ...] [--types]: check the dashboard

Runs Vitest in web/, optionally restricted to the named test files.
With --types, runs TypeScript checking instead of interaction tests.
Install dependencies first with `mop dev web build`.
"""
from mop.cli.dev.web.build import WEB, run, tool


def main(argv):
    npm = tool("npm")
    if argv == ["--types"]:
        run([npm, "exec", "--", "tsc", "--noEmit", "-p", "tsconfig.json"], WEB)
    else:
        run([npm, "test", "--", *argv], WEB)
    return 0
