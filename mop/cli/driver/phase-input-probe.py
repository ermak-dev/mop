"""mop driver phase-input-probe: check private phase handoff in an owned container

Runs the production input writer, root snapshot store and body reader in a new
Python 3.12 Docker container with distinct actual root/node/body UIDs. Checks
exact selected bytes, private permissions, substitution/race refusals, bounded
capacity and retained physical cleanup after Work expiry. Copies only the fixed
source closure; no host mounts, credentials, network, sudo or host services.
Removes only its labelled container/image, including after a failed check.
Requires an existing Docker installation and the mop source checkout with tests.
This checks private transport, not machine authorization, the root backend,
a phase runner/broker, native execution, readiness, installation or rollout.
"""
import asyncio
import json
from pathlib import Path


def main(argv):
    from mop.cli.term import usage
    from mop.node.host_input_probe import run_probe
    if argv:
        usage(__doc__)
    result = asyncio.run(run_probe(Path(__file__).resolve().parents[3]))
    print(json.dumps(result, sort_keys=True))
    return 0
