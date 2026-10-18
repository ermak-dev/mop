"""Владелец новой одноразовой Docker fixture, не production launcher.

Нет host bind mounts, host network, shared PID/cgroup namespace или Docker
socket внутри container. Cleanup касается только UUID-name + собственного label.
"""
import asyncio
import json
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from mop.node.host_scope import Refused, Unknown, canonical
from mop.node.host_scope_systemd import BoundedCommand, CommandFailure

LABEL = 'dev.mop.host-scope-fixture'


def fixture_args(identifier):
    canonical(identifier)
    name = 'mop-host-scope-' + identifier
    return ('create', '--name='+name, '--label='+LABEL+'='+identifier,
            '--privileged', '--cgroupns=private', '--network=none',
            '--tmpfs=/run', '--tmpfs=/run/lock', '--tmpfs=/tmp', name+':probe')


def verify_fixture_config(config):
    if (type(config) is not dict or config.get('Privileged') is not True
            or config.get('PidMode') != '' or config.get('CgroupnsMode') != 'private'
            or config.get('NetworkMode') != 'none' or config.get('Binds') not in (None, [])
            or config.get('Mounts') not in (None, []) or config.get('VolumesFrom') not in (None, [])
            or config.get('CgroupParent') != ''):
        raise Refused('fixture requires actual private PID/cgroup namespace, no network or host mounts')


DOCKERFILE = '''FROM python:3.12-slim-bookworm
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends systemd systemd-sysv dbus passwd python3 > /dev/null && rm -rf /var/lib/apt/lists/*
RUN groupadd -g 21071 fixture-node && useradd -u 21071 -g 21071 -M -s /usr/sbin/nologin fixture-node && groupadd -g 21072 fixture-body && useradd -u 21072 -g 21072 -M -s /usr/sbin/nologin fixture-body
COPY package/ /opt/fixture/
COPY runner.py /opt/mop/libexec/host-runner
COPY entry.py /opt/hostscope-entry.py
COPY acceptance.py /opt/hostscope-acceptance.py
COPY hostbackend-acceptance.py /opt/hostbackend-acceptance.py
RUN chmod -R go-w /opt/fixture && chmod 755 /opt/fixture/bin/mop /opt/mop/libexec/host-runner
ENV container=docker
STOPSIGNAL SIGRTMIN+3
ENTRYPOINT ["/usr/local/bin/python3", "-I", "/opt/hostscope-entry.py"]
'''


def snapshot_sources(source, context):
    """Точный набор fixture sources, без installation inputs и обхода checkout."""
    source, context = Path(source), Path(context)
    package = context/'package/mop/node'
    package.mkdir(parents=True)
    (context/'package/mop/__init__.py').write_text('')
    (package/'__init__.py').write_text('')
    for filename in ('host_scope.py', 'host_scope_client.py', 'host_scope_supervisor.py',
                     'host_scope_systemd.py', 'http_execution.py', 'host_backend.py',
                     'host_backend_client.py', 'host_root.py', 'native_startup.py', 'bootstrap_transport.py'):
        shutil.copyfile(source/'mop/node'/filename, package/filename)
    common = context/'package/mop/common'
    common.mkdir()
    (common/'__init__.py').write_text('')
    shutil.copyfile(source/'mop/common/initialcreate.py', common/'initialcreate.py')
    for name in ('bin/mop', 'mop/cli/__init__.py', 'mop/cli/service/__init__.py',
                 'mop/cli/service/host-root.py'):
        target = context/'package'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source/name, target)
    for src, dst in (('_host_scope_runner.py', 'runner.py'),
                     ('_host_scope_entry.py', 'entry.py'),
                     ('_host_scope_acceptance.py', 'acceptance.py'),
                     ('_host_backend_acceptance.py', 'hostbackend-acceptance.py')):
        shutil.copyfile(source/'tests'/src, context/dst)
    (context/'Dockerfile').write_text(DOCKERFILE)


async def run_probe(source, *, snapshotter=snapshot_sources, acceptance='/opt/hostscope-acceptance.py'):
    """Возвращает actual accepted/blocked; code 77 не является green gate."""
    docker = shutil.which('docker')
    if docker is None:
        return dict(status='blocked', phase='fixture', reason='Docker is not installed')
    identifier = str(uuid4())
    name = 'mop-host-scope-' + identifier
    command = BoundedCommand(timeout=300., limit=262144)
    cleanup = BoundedCommand(timeout=10., limit=65536)
    result = None
    failure = None
    stage = 'image build'
    image_created = container_attempted = False
    cleanup_errors = []
    try:
        with tempfile.TemporaryDirectory(prefix='mop-host-scope-build-') as directory:
            context = Path(directory)
            snapshotter(source, context)
            await command.run(docker, 'build', '--label='+LABEL+'='+identifier,
                              '--tag='+name+':probe', str(context))
            image_created = True
        container_attempted = True
        stage = 'private container creation'
        await command.run(docker, *fixture_args(identifier))
        verify_fixture_config(json.loads(await command.run(docker, 'inspect', '--format={{json .HostConfig}}', name)))
        stage = 'private systemd startup'
        await command.run(docker, 'start', name)
        ready = False
        for _ in range(60):
            state = json.loads(await command.run(docker, 'inspect', '--format={{json .State}}', name))
            if not state['Running']:
                logs = await command.run(docker, 'logs', name)
                for line in logs.decode('utf-8', errors='replace').splitlines():
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if type(value) is dict and value.get('status') == 'blocked':
                        result = value
                        break
                if result is None:
                    raise Unknown('private systemd fixture exited before acceptance (exit '+str(state['ExitCode'])+')')
                break
            try:
                await cleanup.run(docker, 'exec', name, '/usr/bin/test', '-S', '/run/systemd/private')
            except Unknown:
                await asyncio.sleep(.5)
            else:
                ready = True
                break
        if ready:
            try:
                raw = await command.run(docker, 'exec', name, '/usr/local/bin/python3',
                                        '-I', acceptance)
            except Unknown as error:
                # stderr acceptance хранится в bounded capture, не host secret store.
                detail = error.stderr.decode('utf-8', errors='replace')[-8192:] if isinstance(error, CommandFailure) else ''
                raise Unknown('actual OS acceptance failed: '+str(error)+' '+detail) from error
            result = json.loads(raw)
            if type(result) is not dict or result.get('status') != 'accepted':
                raise Unknown('private fixture did not confirm actual containment')
        elif result is None:
            raise Unknown('private systemd startup deadline expired')
    except (OSError, ValueError, Unknown) as error:
        detail = error.stderr.decode('utf-8', errors='replace')[-4096:] if isinstance(error, CommandFailure) else ''
        failure = Unknown(stage+': '+str(error)+(' '+detail if detail else ''))
    finally:
        if container_attempted:
            try:
                names = (await cleanup.run(docker, 'container', 'ls', '--all',
                    '--filter=label='+LABEL+'='+identifier, '--format={{.Names}}')).decode().splitlines()
                if any(value != name for value in names) or len(names) > 1:
                    raise Refused('private fixture label/name mismatch; no foreign removal')
                if names:
                    await cleanup.run(docker, 'rm', '--force', '--volumes', name)
            except (Unknown, Refused) as error:
                cleanup_errors.append(str(error))
        if image_created:
            try:
                label = (await cleanup.run(docker, 'image', 'inspect',
                    '--format={{index .Config.Labels "'+LABEL+'"}}', name+':probe')).decode().strip()
                if label != identifier:
                    raise Refused('private fixture image label mismatch; no foreign removal')
                await cleanup.run(docker, 'image', 'rm', name+':probe')
            except (Unknown, Refused) as error:
                cleanup_errors.append(str(error))
        await command.aclose()
        await cleanup.aclose()
    if cleanup_errors:
        raise Unknown(str(failure or result)+'; private fixture cleanup incomplete: '+'; '.join(cleanup_errors)) from failure
    if failure:
        raise Unknown('HOST scope fixture unavailable or failed: '+str(failure)) from failure
    return result
