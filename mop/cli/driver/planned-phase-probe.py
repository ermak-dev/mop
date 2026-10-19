"""mop driver planned-phase-probe: exercise three planned host phases in an owned fixture

Builds a new Python 3.12/PostgreSQL 17 private systemd container, with actual
root/node/body UIDs, controlled Git HTTPS/SSH remotes and approved Ansible tasks.
No host mounts, host services, installation inputs, pool, deployment or native
execution. Uses the explicit source closure and removes only its owned resources.
Requires an existing Docker installation. A blocked fixture is not acceptance.
"""
import asyncio
import json
from pathlib import Path


def main(argv):
    from mop.cli.term import usage
    from mop.node.host_phase_probe import run_probe
    if argv:
        usage(__doc__)
    result = asyncio.run(run_probe(Path(__file__).resolve().parents[3]))
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get('status') == 'accepted' else 1
