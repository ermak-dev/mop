"""mop driver hold-probe: check the passive installed anchor in an owned container

Runs the production standalone artifact with root and nonroot UIDs in a fresh
Python 3.12 Docker container, with no network, pool, host credentials or writable
host mounts. Checks exact hold argv, signals, hostile environment, bytes/hash,
root ownership, modes and symlink refusals. Root runs only the passive hold,
never a native runtime. Removes the owned container even after a failed check.
Requires an existing Docker installation and a mop source checkout with tests.
This is not an installer, runtime binding, allocation or native readiness check.
"""
from pathlib import Path
import shutil
import subprocess
import uuid


def main(argv):
    from mop.cli.term import usage
    if argv:
        usage(__doc__)
    docker = shutil.which('docker')
    if not docker:
        raise RuntimeError('hold probe requires an existing Docker installation')
    top = Path(__file__).resolve().parents[3]
    fixture = top/'tests/_hold_anchor_fixture.py'
    if not fixture.is_file():
        raise RuntimeError('hold probe requires the mop source checkout with its fixture')
    name = 'mop-hold-probe-' + uuid.uuid4().hex
    try:
        return subprocess.run([docker, 'run', '--rm', '--name', name, '--network', 'none',
            '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'SETUID', '--cap-add', 'SETGID',
            '--cap-add', 'KILL',
            '--pids-limit', '32', '--memory', '128m', '--user', '0:0', '--workdir', '/',
            '--mount', f'type=bind,src={top / "mop/node"},dst=/input,readonly',
            '--mount', f'type=bind,src={fixture},dst=/probe/fixture.py,readonly',
            'python:3.12-slim-trixie', 'python3', '-ISB', '/probe/fixture.py'], check=False).returncode
    finally:
        # Ни wildcard, ни чужие containers: удаляется лишь имя этого вызова.
        listing = subprocess.run([docker, 'container', 'ls', '--all', '--filter',
            'name='+name, '--format', '{{.Names}}'], capture_output=True, text=True)
        if listing.returncode:
            raise RuntimeError('hold probe could not check owned container cleanup')
        if name in listing.stdout.splitlines():
            removed = subprocess.run([docker, 'rm', '-f', name], capture_output=True)
            if removed.returncode:
                raise RuntimeError('hold probe could not remove its owned container')
