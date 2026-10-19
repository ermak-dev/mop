"""Owned Python 3.12 root/node/body fixture: только private transport (#746).

Нет host mounts, network, sudo, services, secret inputs или native execution.
Source closure конечный и явный; cleanup только UUID name + собственный label.
"""
import json
from pathlib import Path
import re
import shutil
import tempfile
from uuid import uuid4

from mop.node.host_scope import Refused, Unknown, canonical
from mop.node.host_scope_systemd import BoundedCommand, CommandFailure

LABEL = 'dev.mop.phase-input-fixture'
SOURCES = ('mop/node/host_inputs.py', 'mop/node/host_input_files.py',
    'mop/node/host_scope.py', 'mop/node/http_execution.py',
    'mop/common/initialcreate.py', 'mop/common/bootstrap_envelope.py',
    'mop/common/initial_transfer.py',
    'mop/common/credentials.py', 'mop/common/soul.py',
    'mop/common/busnames.py', 'mop/common/paths.py',
    'tests/_host_inputs_data.py', 'tests/_host_inputs_fixture.py')
DOCKERFILE = '''FROM python:3.12-slim-trixie
COPY mop/ /opt/fixture/mop/
COPY tests/ /opt/fixture/tests/
ENTRYPOINT ["/usr/local/bin/python3", "-ISB", "/opt/fixture/tests/_host_inputs_fixture.py"]
'''


def snapshot_sources(source, context):
    source, context = Path(source), Path(context)
    for name in SOURCES:
        target = context/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source/name, target)
    for name in ('mop/__init__.py', 'mop/common/__init__.py', 'mop/node/__init__.py'):
        (context/name).write_text('')
    (context/'Dockerfile').write_text(DOCKERFILE)


def fixture_args(identifier):
    canonical(identifier)
    name = 'mop-phase-input-'+identifier
    return ('create', '--name='+name, '--label='+LABEL+'='+identifier, '--network=none',
        '--cap-drop=ALL', '--cap-add=CHOWN', '--cap-add=SETUID', '--cap-add=SETGID',
        '--cap-add=DAC_OVERRIDE', '--cap-add=FOWNER', '--security-opt=no-new-privileges',
        '--pids-limit=32', '--memory=128m', '--user=0:0', '--read-only',
        '--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777', name+':probe')


def verify_fixture_config(value):
    if (type(value) is not dict or value.get('Privileged') is not False
            or value.get('NetworkMode') != 'none' or value.get('PidMode') != ''
            or value.get('Binds') not in (None, []) or value.get('Mounts') not in (None, [])
            or value.get('VolumesFrom') not in (None, []) or value.get('ReadonlyRootfs') is not True):
        raise Refused('phase input fixture requires private read-only container without host mounts or network')


def safe_failure(stderr):
    """Только имена finite fixture checks, не raw assertion/traceback/secret."""
    names = re.findall(rb'^(?:FAIL|ERROR): (test_[a-z0-9_]{1,160}) \(', stderr, re.MULTILINE)
    return 'owned fixture checks failed: '+', '.join(name.decode('ascii') for name in names[:32]) \
        if names else 'owned fixture command failed without a safe check diagnostic'


async def run_probe(source):
    docker = shutil.which('docker')
    if docker is None:
        raise Unknown('phase input probe requires an existing Docker installation')
    identifier = str(uuid4())
    name = 'mop-phase-input-'+identifier
    command, cleanup = BoundedCommand(timeout=10., limit=65536), BoundedCommand(timeout=10., limit=65536)
    image_attempted = container_attempted = False
    failure = None
    result = None
    stage = 'source snapshot'
    cleanup_errors = []
    try:
        with tempfile.TemporaryDirectory(prefix='mop-phase-input-build-') as directory:
            snapshot_sources(source, directory)
            stage = 'image build'
            image_attempted = True
            await command.run(docker, 'build', '--network=none', '--pull=false',
                '--label='+LABEL+'='+identifier, '--tag='+name+':probe', directory)
        stage = 'private container creation'
        container_attempted = True
        await command.run(docker, *fixture_args(identifier))
        verify_fixture_config(json.loads(await command.run(docker, 'inspect', '--format={{json .HostConfig}}', name)))
        stage = 'actual UID acceptance'
        raw = await command.run(docker, 'start', '--attach', name)
        result = json.loads(raw)
        expected = dict(status='accepted', python='3.12', root_uid=0, node_uid=21071, body_uid=21072,
                        transport_only=True, native_execution=False)
        if type(result) is not dict or any(result.get(key) != value for key, value in expected.items()):
            raise Refused('phase input fixture did not confirm actual UID acceptance')
    except (OSError, ValueError, Unknown) as error:
        # Raw capture содержит только synthetic fixture, но всё равно не отражается.
        reason = safe_failure(error.stderr) if isinstance(error, CommandFailure) else str(error)
        failure = Unknown('phase input probe '+stage+': '+reason)
    finally:
        if container_attempted:
            try:
                names = (await cleanup.run(docker, 'container', 'ls', '--all',
                    '--filter=label='+LABEL+'='+identifier, '--format={{.Names}}')).decode().splitlines()
                if any(value != name for value in names) or len(names) > 1:
                    raise Refused('phase input fixture label/name mismatch; no foreign removal')
                if names:
                    await cleanup.run(docker, 'rm', '--force', '--volumes', name)
            except (Unknown, Refused) as error:
                cleanup_errors.append(str(error))
        if image_attempted:
            try:
                # Поиск по точному label также покрывает failed/unknown build ACK.
                images = (await cleanup.run(docker, 'image', 'ls',
                    '--filter=label='+LABEL+'='+identifier, '--format={{.Repository}}:{{.Tag}}')).decode().splitlines()
                if any(value != name+':probe' for value in images) or len(images) > 1:
                    raise Refused('phase input fixture image label/tag mismatch; no foreign removal')
                if images:
                    await cleanup.run(docker, 'image', 'rm', name+':probe')
            except (Unknown, Refused) as error:
                cleanup_errors.append(str(error))
        await command.aclose()
        await cleanup.aclose()
    if cleanup_errors:
        raise Unknown('phase input fixture cleanup incomplete: '+'; '.join(cleanup_errors)) from None
    if failure:
        raise failure
    return result
