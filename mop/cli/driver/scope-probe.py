"""mop driver scope-probe: check physical host containment in a new private Docker fixture

Builds an isolated systemd fixture with private PID/cgroup namespaces, no host
bind mounts, no Docker socket and no network. Runs only the fixture's installed
double-fork runner, never an existing body, host service or user session.

Exit 0 is actual OS containment acceptance; exit 77 means the kernel could not
provide the private fixture. Neither outcome binds production capabilities or
installs a supervisor. Requires an already available Docker installation.
"""
import asyncio
import json
from pathlib import Path

from mop.node.host_scope_probe import run_probe


def main(argv):
    if argv:
        raise RuntimeError('use mop driver scope-probe without arguments')
    result = asyncio.run(run_probe(Path(__file__).resolve().parents[3]))
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'accepted' else 77
