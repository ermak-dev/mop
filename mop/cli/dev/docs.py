"""mop dev docs: regenerate docs/CLI.md from the commandlets' docstrings

Writes the CLI reference: every command and verb of `mop` with its whole
docstring, in three parts -- client, server, service. Silent when the file
is written. A commandlet with an empty docstring is refused, named.

The reference is committed: an edit to a commandlet's docstring ships with
the regenerated docs/CLI.md in the same commit, or CI is red.
"""
from mop import cli
# Без mop.cli.lib: lib тянет шину, а справочнику нужны только файлы (#302).
from mop.cli.term import fail, usage


def main(argv):
    if argv:
        usage(__doc__)
    try:
        text = cli.reference()
    except ValueError as e:
        fail(str(e))
        return 1
    with open(cli.REFERENCE, "w") as f:
        f.write(text)
    return 0
