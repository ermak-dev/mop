"""push claude.ai credentials and LLM keys to pool nodes: mop login

The `write` verb to every node's agent at once: the node keeps a copy for
bodies raised later, and every live body gets it now. Silent when every node
took them; otherwise the nodes that did not, and why. A node whose agent does
not answer is not reached: there is no way past the agent (#135).
"""
import sys

from mop.cli import lib
from mop import keys


def main(argv):
    if argv:
        lib.usage(__doc__)
    results, _what, note = keys.push_login()
    if note:
        print(f"LLM keys: {note}", file=sys.stderr)
    bad = {n: r for n, r in results.items() if r != "OK"}
    for node in sorted(bad):
        lib.fail(f"{node}: {bad[node]}")
    return 1 if bad else 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
