"""mop dev controller [--postgres 17] [--check controller|controller_runtime|hermetic|preparation|telemetry|credential_service|usage|fresh_postgres|client] [--label dotted.test] [--realtime] [--cross-uid] [--web]: isolated PostgreSQL checks

Runs the selected check on Python 3.12 and PostgreSQL 17 with the declared server
extra and matching client tools in fresh Docker containers, using the same setup as CI. Requires
an existing Docker installation and a mop Git working copy; installs no host
packages and uses no pool. The client check runs clean-wheel HTTPS checks and
native Django/PostgreSQL client contracts. The snapshot contains tracked and
nonignored source, not installation inputs or .git. No host ports, installation credentials or
Docker socket enter test containers. Cleanup failures are reported explicitly.
--realtime supplies the declared NATS binary from an owned Docker image; the
checks start their own native TLS broker with fixture-only principals and ACLs.
--cross-uid runs only credential_service as root inside the owned container to
exercise distinct OS peers; the default suite still runs as unprivileged ci.
--web adds the frontend gates — the dashboard build check, full TypeScript
checking and the complete Vitest interaction suite — with Node tools in the
same Python 3.12 container; every check runs under its own exit status and any
failure rejects the fixture. It cannot be combined with the narrow root-only
--cross-uid mode.
"""
import argparse
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile
import uuid


def _source_snapshot(top, destination):
    try:
        files = subprocess.run(['git', '-C', str(top), 'ls-files', '--cached', '--others',
                                '--exclude-standard', '-z'], check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError):
        raise RuntimeError('isolated controller checks require a readable mop Git working copy') from None
    destination.mkdir()
    excluded = {'.git', '.claude', '__pycache__', 'node_modules', 'build'}
    for name in set(files.split(b'\0')) - {b''}:
        relative = Path(os.fsdecode(name))
        if relative.is_absolute() or '..' in relative.parts:
            raise RuntimeError('source snapshot contains an invalid relative path')
        if set(relative.parts) & excluded or relative.name.endswith('.egg-info'):
            continue
        if len(relative.parts) == 1 and relative.name in {'.env', 'inventory.yaml', 'sandbox.yaml'}:
            continue
        source = top / relative
        if not source.exists() and not source.is_symlink():
            continue
        if source.is_dir() and not source.is_symlink():
            raise RuntimeError('source snapshot requires explicit files, not unresolved submodules')
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres', choices=('17',), default='17')
    parser.add_argument('--check', choices=('controller', 'controller_runtime', 'hermetic', 'preparation', 'telemetry', 'credential_service', 'usage', 'fresh_postgres', 'client'), default='controller')
    parser.add_argument('--label', action='append', default=[], help='Django test label; repeat for several checks')
    parser.add_argument('--realtime', action='store_true', help='include the owned native TLS NATS fixture')
    parser.add_argument('--cross-uid', action='store_true')
    parser.add_argument('--web', action='store_true', help='include the dashboard gate in the same Python container')
    args = parser.parse_args(argv)
    if args.cross_uid and args.web:
        parser.error('--web cannot extend the root-only credential fixture')
    if args.cross_uid and args.check != 'credential_service':
        parser.error('--cross-uid requires --check credential_service')
    if args.label and args.check != 'controller':
        parser.error('--label requires --check controller')
    check_script = {'usage': 'proxy_usage_store', 'client': 'control_tls'}.get(args.check, args.check)
    test_script = shlex.join(['python3', f'tests/{check_script}.py', *args.label])
    if args.check == 'client':
        test_script += ' && ' + shlex.join(['python3', 'tests/controller.py', '_control_client_cases'])
    if args.web:
        test_script = ('python_status=0; bash -c ' + shlex.quote(test_script) + ' || python_status=$?; '
            'web_status=0; python3 tests/_web_dist_fixture.py || web_status=$?; '
            'test "$python_status" -eq 0 && test "$web_status" -eq 0')
    docker = shutil.which('docker')
    if not docker:
        raise RuntimeError('isolated controller checks require an existing Docker installation')
    from .test import root
    import yaml
    top = Path(root())
    with (top / '.gitlab-ci.yml').open() as stream:
        ci = yaml.safe_load(stream)
    job = ci['test:py3.12']['variables']
    setup = ci['.tests']['variables']['SETUP']['value']
    if args.cross_uid:
        command = 'exec su ci -s /bin/bash -c "$TESTS"'
        if setup.count(command) != 1 or not setup.rstrip().endswith(command):
            raise RuntimeError('cross-UID fixture requires the declared final ci test command')
        setup = setup.replace(command, 'exec bash -c "$TESTS"')
    prefix = 'mop-controller-test-' + uuid.uuid4().hex[:16]
    database, worker = prefix + '-db', prefix + '-runner'
    broker_image = prefix + '-nats-image'
    node_image = prefix + '-node-image'
    cleanup_errors = []

    def call(words, *, check=True, **kwargs):
        return subprocess.run([docker, *words], check=check, **kwargs)

    def cleanup(words, label):
        try:
            result = call(words, check=False, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.SubprocessError):
            cleanup_errors.append(label)
        else:
            if result.returncode:
                cleanup_errors.append(label)

    def remove_owned(kind, name):
        listing = ['container', 'ls', '--all'] if kind == 'container' else ['network', 'ls']
        try:
            result = call([*listing, '--filter', 'name=' + name, '--format', '{{.Names}}' if
                           kind == 'container' else '{{.Name}}'], check=False,
                          capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            cleanup_errors.append(kind + ' lookup')
            return
        if result.returncode:
            cleanup_errors.append(kind + ' lookup')
        elif name in result.stdout.splitlines():
            words = ['rm', '-f', '-v', name] if kind == 'container' else ['network', 'rm', name]
            cleanup(words, kind + ' removal')

    result, failure, interrupted = None, None, False
    with tempfile.TemporaryDirectory(prefix=prefix) as directory:
        snapshot = Path(directory) / 'source'
        _source_snapshot(top, snapshot)
        broker_args = []
        node_args = []
        try:
            if args.web:
                tools = Path(directory) / 'node'
                (tools / 'bin').mkdir(parents=True)
                (tools / 'lib' / 'node_modules').mkdir(parents=True)
                call(['create', '--name', node_image, job['NODE_IMAGE']], stdout=subprocess.DEVNULL)
                call(['cp', node_image + ':/usr/local/bin/node', str(tools / 'bin' / 'node')])
                call(['cp', node_image + ':/usr/local/lib/node_modules/npm', str(tools / 'lib' / 'node_modules') + '/'])
                (tools / 'bin' / 'npm').symlink_to('../lib/node_modules/npm/bin/npm-cli.js')
                (tools / 'bin' / 'npx').symlink_to('../lib/node_modules/npm/bin/npx-cli.js')
                for path in (tools, tools / 'bin', tools / 'lib', tools / 'lib' / 'node_modules'):
                    path.chmod(0o755)
                node_args = ['-v', f'{tools}:/opt/mop-node:ro',
                             '-v', 'mop-controller-fixture-npm:/tmp/ci-home/.npm']
            if args.realtime:
                binary = Path(directory) / 'nats'
                binary.mkdir()
                call(['create', '--name', broker_image, 'nats:2.10.26'], stdout=subprocess.DEVNULL)
                call(['cp', broker_image + ':/nats-server', str(binary / 'nats-server')])
                broker_args = ['-v', f'{binary}:/opt/mop-nats:ro', '-e',
                               'MOP_TEST_NATS_BINARY=/opt/mop-nats/nats-server']
            call(['network', 'create', prefix], stdout=subprocess.DEVNULL)
            call(['run', '--rm', '-d', '--name', database, '--network', prefix,
                  '--network-alias', 'postgres', '-e', 'POSTGRES_DB=mop_ci',
                  '-e', 'POSTGRES_USER=mop_ci', '-e', 'POSTGRES_PASSWORD=isolated-test-only',
                  job['POSTGRES_IMAGE'], 'bash', '-euc',
                  'openssl req -x509 -newkey rsa:2048 -nodes -days 2 '
                  '-subj /CN=postgres -addext subjectAltName=DNS:postgres '
                  '-keyout /tmp/mop-test-server.key -out /tmp/mop-test-server.crt >/dev/null 2>&1; '
                  'chmod 600 /tmp/mop-test-server.key; '
                  'chown postgres:postgres /tmp/mop-test-server.key /tmp/mop-test-server.crt; '
                  'exec docker-entrypoint.sh postgres -c ssl=on '
                  '-c ssl_cert_file=/tmp/mop-test-server.crt -c ssl_key_file=/tmp/mop-test-server.key'],
                 stdout=subprocess.DEVNULL)
            call(['exec', database, 'bash', '-ec',
                  'for attempt in $(seq 1 60); do '
                  'test -s /tmp/mop-test-server.crt && pg_isready -U mop_ci && exit 0; sleep 1; done; exit 1'],
                 stdout=subprocess.DEVNULL)
            call(['cp', database + ':/tmp/mop-test-server.crt', str(snapshot / '.test-postgres-ca.pem')])
            # Test script, не subprocess другого командлета.
            result = call(['run', '--rm', '--name', worker, '--network', prefix,
                           '-v', f'{snapshot}:/io', '-w', '/io',
                           '-v', 'mop-controller-fixture-pip:/root/.cache/pip',
                           '-e', 'MOP_TESTS_STRICT=1', '-e', 'MOP_TEST_POSTGRES_HOST=postgres',
                           '-e', f'POSTGRES_MAJOR={args.postgres}',
                           '-e', f'TESTS={test_script}', *broker_args, *node_args,
                           job['PYTHON_IMAGE'], 'bash', '-euc', setup], check=False).returncode
        except OSError as error:
            failure = f'isolated controller fixture unavailable: {type(error).__name__}'
        except subprocess.CalledProcessError as error:
            failure = f'isolated controller fixture step failed with exit {error.returncode}'
        except KeyboardInterrupt:
            interrupted = True
        finally:
            # --rm мог уже убрать завершённый runner: отсутствие — успех,
            # а недоступный daemon нельзя принять за отсутствие ресурса.
            remove_owned('container', broker_image)
            remove_owned('container', node_image)
            remove_owned('container', worker)
            remove_owned('container', database)
            remove_owned('network', prefix)
            cleanup(['run', '--rm', '-v', f'{snapshot}:/io', 'alpine',
                     'chown', '-R', f'{os.getuid()}:{os.getgid()}', '/io'], 'snapshot ownership')
    if cleanup_errors:
        primary = failure or ('fixture interrupted' if interrupted else f'fixture check exit {result}')
        raise RuntimeError(primary + '; cleanup incomplete: ' + ', '.join(cleanup_errors))
    if interrupted:
        raise KeyboardInterrupt
    if failure:
        raise RuntimeError(failure)
    return result
