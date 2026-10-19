"""Узкий root-only gate реальных перекрёстных UID (#674).

Ровно два случая #669 требуют исполнять клиент под другим OS UID (65534) --
настоящий SO_PEERCRED, не мок. В общем непривилегированном прогоне они
жили пропуском, и CI молча считал их зелёными. Здесь они перенесены без
изменения утверждений и исполняются только точечными метками через
tests/_credential_uid_fixture.py в owned Docker fixture под root.

Модуль -- только определение случаев: классы наследуют setUp общих
модулей, но gate выбирает два перенесённых метода, не весь класс,
и один fresh-install startup #644 под отдельным непривилегированным UID.
"""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import UUID

from asgiref.sync import async_to_sync
from django.test import TransactionTestCase, override_settings

import _credential_cases as socket_cases
import _credential_binding_cases as binding_cases


class CredentialSocketUids(socket_cases.CredentialSocketCases):
    """Root-only подмножество CredentialSocketCases: один перенесённый случай."""

    def test_distinct_frontend_uid_uses_only_group_protected_socket(self):
        if os.geteuid() != 0:
            raise AssertionError('cross-UID gate requires the owned root container')
        import sys
        from mop.server.controller.runtime import Runtime
        shared = tempfile.TemporaryDirectory(prefix='mop-credential-crossuid-', dir='/tmp')
        self.addCleanup(shared.cleanup)
        self.path = str(Path(shared.name) / 'credential.sock')
        os.chmod(shared.name, 0o750)
        code = '''
import json, os, sys
sys.path.insert(0, sys.argv[1])
from mop.common.credential_client import CredentialClient
from mop.common.credentials import Credential
from uuid import UUID
payload = json.loads(sys.stdin.read())
assert os.geteuid() == 65534
client = CredentialClient(payload['path'], server_uid=0)
item = client.create(Credential('token', payload['token']), project_id=UUID(payload['project']),
    name='DISTINCT_UID', kind='variable', purpose='project', value=b'uid-fixture')
assert not any(name.startswith(('django', 'mop.server', 'nacl')) for name in sys.modules)
print(item.revision)
'''
        with override_settings(BOOTSTRAP=self.bootstrap):
            app = self.service.application()
        async def run():
            runtime = Runtime(queue_limit=0)
            server = self.service.CredentialService(self.path, application=app, runtime=runtime,
                allowed_uids=(65534,), socket_group=os.getegid())
            await server.start()
            try:
                process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', code,
                    str(Path(__file__).resolve().parents[1]), user=65534, group=os.getegid(), extra_groups=(),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env={key: value for key, value in os.environ.items() if not key.startswith(('MOP_', 'DJANGO_'))})
                payload = json.dumps({'path': self.path, 'token': self.token, 'project': str(self.project.id)}).encode()
                async with asyncio.timeout(8):
                    output, error = await process.communicate(payload)
                self.assertEqual(process.returncode, 0, error.decode())
                self.assertEqual(output.strip(), b'1')
                client = self.Client(self.path, server_uid=0)
                with self.assertRaises(self.Error):
                    await asyncio.to_thread(self.create, client)
            finally:
                await server.aclose()
                await runtime.aclose()
        async_to_sync(run)()


class AddressedCredentialUids(binding_cases.AddressedCredentialCases):
    """Root-only подмножество AddressedCredentialCases: один перенесённый случай."""

    def test_addressed_delivery_uses_actual_frontend_uid_without_rights_inference(self):
        if os.geteuid() != 0:
            raise AssertionError('cross-UID gate requires the owned root container')
        import sys
        from mop.server.controller.runtime import Runtime
        self.delivery()
        shared = tempfile.TemporaryDirectory(prefix='mop-addressed-crossuid-', dir='/tmp')
        self.addCleanup(shared.cleanup)
        self.path = str(Path(shared.name) / 'credential.sock')
        os.chmod(shared.name, 0o750)
        with override_settings(BOOTSTRAP=self.bootstrap):
            app = self.service.application()
        code = '''
import json, os, sys
sys.path.insert(0, sys.argv[1])
from mop.common.credential_client import CredentialClient
from mop.common.credentials import Credential, CredentialError
from uuid import UUID
p = json.loads(sys.stdin.read())
assert os.geteuid() == 65534
client = CredentialClient(p['path'], server_uid=0)
rows = client.issue_addressed(Credential('token', p['machine']), target_id=UUID(p['target']))
assert len(rows) == 1 and rows[0].value == b'exact-addressed-value'
try:
    client.issue_addressed(Credential('token', p['human']), target_id=UUID(p['target']))
except CredentialError:
    pass
else:
    raise AssertionError('peer UID gave human node authority')
assert not any(n.startswith(('django', 'mop.server', 'nacl')) for n in sys.modules)
print('addressed-only')
'''
        async def run():
            runtime = Runtime(queue_limit=0)
            server = self.service.CredentialService(self.path, application=app, runtime=runtime,
                allowed_uids=(65534,), socket_group=os.getegid())
            await server.start()
            try:
                process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', code,
                    str(Path(__file__).resolve().parents[1]), user=65534, group=os.getegid(), extra_groups=(),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env={k: v for k, v in os.environ.items() if not k.startswith(('MOP_', 'DJANGO_'))})
                async with asyncio.timeout(10):
                    output, error = await process.communicate(json.dumps({'path': self.path,
                        'machine': self.machine_auth.value, 'human': self.token,
                        'target': str(self.assignment.id)}).encode())
                self.assertEqual(process.returncode, 0, error.decode())
                self.assertEqual(output.strip(), b'addressed-only')
            finally:
                await server.aclose()
                await runtime.aclose()
        async_to_sync(run)()


class ManagementContextUids(TransactionTestCase):
    """Тот же actual UID gate, original inventory остаётся неизменным."""
    serialized_rollback = True
    import _management_context_cases as management
    setUp = management.ManagementContext.setUp
    fixture = management.ManagementContext.fixture
    delivery = management.ManagementContext.delivery
    reserved_grant = management.ManagementContext.reserved_grant
    grant_assignment = management.ManagementContext.grant_assignment
    enroll = management.ManagementContext.enroll

    def test_management_context_actual_frontend_uid_has_no_human_or_generic_token_authority(self):
        if os.geteuid() != 0:
            raise AssertionError('cross-UID gate requires the owned root container')
        import sys
        from mop.server.controller.runtime import Runtime
        self.fixture()
        shared = tempfile.TemporaryDirectory(prefix='mop-management-crossuid-', dir='/tmp')
        self.addCleanup(shared.cleanup)
        self.path = str(Path(shared.name) / 'credential.sock')
        os.chmod(shared.name, 0o750)
        with override_settings(BOOTSTRAP=self.bootstrap):
            app = self.service.application()
        code = '''
import json, os, sys
sys.path.insert(0, sys.argv[1])
from mop.common.credential_client import CredentialClient
from mop.common.credentials import Credential, CredentialError
p = json.loads(sys.stdin.read())
assert os.geteuid() == 65534
client = CredentialClient(p['path'], server_uid=0)
value = client.read_management_context(Credential('token', p['machine']), p['request'])
assert value.token == p['canonical'] and value.subject_id == p['subject']
assert p['canonical'] not in repr(value)
for auth, request in ((p['human'], p['request']),
        (p['machine'], {**p['request'], 'actor_id': p['subject']}),
        (p['machine'], {**p['request'], 'origin': 'https://foreign.invalid'})):
    try:
        client.read_management_context(Credential('token', auth), request)
    except (CredentialError, ValueError):
        pass
    else:
        raise AssertionError('peer UID supplied management authority')
assert not any(n.startswith(('django', 'mop.server', 'nacl')) for n in sys.modules)
print('exact-active-agent-only')
'''
        async def run():
            runtime = Runtime(queue_limit=0)
            server = self.service.CredentialService(self.path, application=app, runtime=runtime,
                allowed_uids=(65534,), socket_group=os.getegid())
            await server.start()
            try:
                process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', code,
                    str(Path(__file__).resolve().parents[1]), user=65534, group=os.getegid(), extra_groups=(),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env={k: v for k, v in os.environ.items() if not k.startswith(('MOP_', 'DJANGO_'))})
                async with asyncio.timeout(10):
                    output, error = await process.communicate(json.dumps({'path': self.path,
                        'machine': self.machine_auth.value, 'human': self.token, 'request': self.request,
                        'canonical': self.subject_token, 'subject': self.subject.id}).encode())
                self.assertEqual(process.returncode, 0, error.decode())
                self.assertEqual(output.strip(), b'exact-active-agent-only')
            finally:
                await server.aclose()
                await runtime.aclose()
        async_to_sync(run)()


class InstalledCredentialUids(unittest.TestCase):
    """Fresh installer -> настоящий rootless daemon, не подмена application/provider."""
    # HYPOTHESIS: renderer и OS files не дают реального отдельного credential LOGIN/startup.
    # SPEC: production command, PG17 verify-full, singular purpose/plural OS identity;
    # group socket с точными peers, SIGTERM cleanup и повторный старт без stale deletion.
    # SOLUTION: повторно используем installer и существующий credentials lifecycle.
    # RESULT: PG17 RED — four-password restriction; GREEN — rootless startup/mutation/SIGTERM/restart.
    # STATUS: FIXED — see #644 (owned fixture; не systemd/deploy/worker acceptance).
    def test_fresh_installer_starts_rootless_daemon_with_separate_pg_login_and_frontend_uid(self):
        if os.geteuid() != 0:
            raise AssertionError('cross-UID gate requires the owned root container')
        import pwd
        import shlex
        import signal
        import subprocess
        import sys
        import time
        from dataclasses import replace
        from _installation_cases import FreshPostgreSQL
        from mop.server import installation
        from mop.common.credential_client import CredentialClient
        from mop.common.credentials import Credential, CredentialError
        FreshPostgreSQL.setUp(self)
        peer, worker, service = (pwd.getpwnam(name) for name in ('ci', 'daemon', 'nobody'))
        self.assertEqual(len({peer.pw_uid, worker.pw_uid, service.pw_uid}), 3)
        shared = tempfile.TemporaryDirectory(prefix='mop-installed-credential-', dir='/tmp')
        self.addCleanup(shared.cleanup)
        base = Path(shared.name)
        base.chmod(0o755)
        ca = base / 'postgres-ca.pem'
        ca.write_bytes(Path(dict(self.spec.database)['tls_ca']).read_bytes())
        ca.chmod(0o644)
        users = dict(self.spec.identities)
        users.update(credentials=service.pw_name, web=peer.pw_name, worker=worker.pw_name)
        database = {**dict(self.spec.database), 'tls_ca': str(ca)}
        # Новый общий CA path должен совпасть и с реальным admin connection,
        # иначе provisioner правильно отвергает несогласованную topology.
        import psycopg
        from django.conf import settings
        parameters = self.admin.info.get_parameters()
        parameters.update(sslrootcert=str(ca), password=settings.BOOTSTRAP['database']['PASSWORD'])
        self.admin.close()
        self.admin = psycopg.connect(**parameters, autocommit=True)
        paths = {name: str(base / name) for name in dict(self.spec.paths)}
        # #807: интерпретатор установки -- объявленный venv, не литерал; здесь
        # это окружение, в котором уже стоят зависимости ворот.
        runtime = {**dict(self.spec.runtime), 'venv': sys.prefix}
        self.users = users
        self.spec = replace(self.spec, identities=tuple(users.items()),
                            paths=tuple(paths.items()), runtime=tuple(runtime.items()),
                            database=tuple(database.items()))
        FreshPostgreSQL.provision(self)
        for name in ('configuration', 'state'):
            Path(paths[name]).mkdir(mode=0o755)
        root = b'fresh-owned-644-encryption-root-fixture'
        signing = b'fresh-owned-644-django-signing-fixture' * 2
        for role in ('maintenance', 'credentials'):
            directory = Path(paths['configuration']) / role
            directory.mkdir(mode=0o700)
            for name, value in [('django.key', signing), ('root.key', root)]:
                file = directory / name
                file.write_bytes(value)
                file.chmod(0o600)
                if role == 'credentials':
                    os.chown(file, service.pw_uid, service.pw_gid)
            if role == 'credentials':
                os.chown(directory, service.pw_uid, service.pw_gid)
        Path(paths['root_trust']).mkdir(mode=0o700)
        (Path(paths['root_trust']) / 'root.key').write_bytes(root)
        (Path(paths['root_trust']) / 'root.key').chmod(0o600)
        state = Path(paths['state']) / 'credentials'
        state.mkdir(mode=0o700)
        os.chown(state, service.pw_uid, service.pw_gid)
        socket_dir = Path(installation.credential_socket_directory(self.spec))
        socket_dir.mkdir(mode=0o750)
        os.chown(socket_dir, service.pw_uid, service.pw_gid)
        maintenance = Path(paths['configuration']) / 'maintenance' / 'bootstrap.json'
        maintenance.write_text(json.dumps(installation.bootstrap_document(self.spec, 'maintenance',
                                                    password=self.passwords['maintenance'])))
        maintenance.chmod(0o600)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('MOP_', 'DJANGO_', 'PG')) and key != 'TMPDIR'}
        # libpq не должен искать client certificate в private HOME runner-а;
        # собственный traversable HOME fixture не содержит TLS client keys.
        env['HOME'] = str(base)
        # #754: диагностируем настоящий installed seed, не заменяя ORM/runtime.
        # stdout — только private JSON с token; ни timeout, ни ошибка его не выводят.
        code = '''
import json, sys, time, traceback
from contextlib import contextmanager
from pathlib import Path
started = time.monotonic()
def diagnostic(phase, event, **metadata):
    print(json.dumps(dict(phase=phase, event=event, elapsed=round(time.monotonic()-started, 3),
                          **metadata)), file=sys.stderr, flush=True)
@contextmanager
def phase(name):
    diagnostic(name, 'start')
    try:
        yield
    except BaseException as error:
        cause = error
        while cause.__cause__ is not None:
            cause = cause.__cause__
        diagnostic(name, 'error', error_type=type(error).__name__,
                   sqlstate=getattr(cause, 'sqlstate', None))
        raise
    else:
        diagnostic(name, 'done')
def stacks(signum, frame):
    diagnostic('threads', 'snapshot', stacks=[[
        [Path(item.filename).name, item.name, item.lineno]
        for item in traceback.extract_stack(current)] for current in sys._current_frames().values()])
import signal
signal.signal(signal.SIGUSR1, stacks)
with phase('import-runtime'):
    from mop.server.controller import runtime
with phase('configure'):
    runtime.configure()
original_maintenance, original_close = runtime.maintenance, runtime.close_pools
def close_pools():
    with phase('close-pools'):
        original_close()
def maintenance(action, **kwargs):
    with phase('maintenance-' + action):
        return original_maintenance(action, **kwargs)
runtime.maintenance, runtime.close_pools = maintenance, close_pools
try:
    with phase('prepare-database'):
        from mop.server.installation import prepare_database
        prepare_database()
    with phase('import-models'):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Permission
        from mop.server.access import models as m, services as access
        from mop.server.catalog.models import Project
    with phase('create-user'):
        user = get_user_model().objects.create_user('installed-credential-user', password='fixture-only-password')
    with phase('create-identity'):
        source = m.IdentitySource.objects.create(name='local', kind='local', priority=0)
        m.LoginIdentity.objects.create(user=user, source=source, external_subject=user.username)
    with phase('create-project'):
        project = Project.objects.create(name='installed-project', origin='https://example.invalid/installed.git')
        m.ProjectMembership.objects.create(user=user, source=source, project=project)
    with phase('add-permission'):
        user.user_permissions.add(Permission.objects.get(content_type__app_label='secrets', codename='manage_project_secret'))
    with phase('issue-token'):
        auth = {'token': access.issue_token(user.pk), 'project': str(project.pk)}
finally:
    close_pools()
print(json.dumps(auth))
'''
        from _credential_uid_seed import run_seed
        def owned_activity():
            # Только sessions нашей свежесозданной БД, никогда SQL/DSN/чужие queries.
            # Observer тоже ограничен transport/connect 5 s и SQL/lock 1 s;
            # отдельное read-only соединение не меняет provisioning authority.
            from mop.server.controller.postgresql.base import BoundedConnection
            observer_parameters = {**parameters, 'options': '-c default_transaction_read_only=on '
                '-c statement_timeout=1000 -c lock_timeout=1000', 'connect_timeout': 5}
            with BoundedConnection.connect(**observer_parameters, autocommit=True) as observer:
                return observer.execute('SELECT pid, state, wait_event_type, wait_event, '
                    'pg_blocking_pids(pid) FROM pg_stat_activity WHERE datname = %s', (self.dbname,)).fetchall()
        auth = run_seed([sys.executable, '-I', '-c', code], cwd=base,
            env={**env, 'MOP_CONTROLLER_BOOTSTRAP': str(maintenance)}, activity=owned_activity)
        private = Path(paths['configuration']) / 'credentials'
        spec_file = private / 'spec.json'
        from mop.server.install_provision import document
        spec_file.write_text(json.dumps(document(self.spec)))
        password_file = private / 'database.password'
        password_file.write_text(self.passwords['credential'])
        for file in (spec_file, password_file):
            file.chmod(0o600)
            os.chown(file, service.pw_uid, service.pw_gid)
        command = [sys.prefix + '/bin/mop', 'server', 'install', 'bootstrap', '--spec', str(spec_file),
                   '--role', 'credential', '--password-file', str(password_file)]
        boot = subprocess.run(command, cwd=state, env=env, user=service.pw_uid, group=service.pw_gid,
                              extra_groups=(), capture_output=True, text=True, timeout=20)
        self.assertEqual(boot.returncode, 0, boot.stderr)
        self.assertEqual(boot.stdout, '')
        bootstrap = private / 'bootstrap.json'
        self.assertEqual(bootstrap.stat().st_uid, service.pw_uid)
        self.assertEqual(bootstrap.stat().st_mode & 0o777, 0o600)
        values = {'public_origin': 'https://fixture.invalid', 'http_bind': '127.0.0.1', 'http_port': 9443,
                  'db_pool_max': 8, 'instance_db_connections': 24, 'admin_db_headroom': 8}
        artifact = installation.service_artifact(self.spec, values, credential_uid=service.pw_uid,
            credential_gid=service.pw_gid, frontend_uids={'web': peer.pw_uid, 'worker': worker.pw_uid})
        unit = artifact['units']['credentials']
        argv = shlex.split(next(line.removeprefix('ExecStart=') for line in unit.splitlines()
                                if line.startswith('ExecStart=')))
        socket_path = socket_dir / 'service.sock'
        client_code = '''
import json, os, sys
from uuid import UUID
from mop.common.credential_client import CredentialClient
from mop.common.credentials import Credential
p = json.loads(sys.stdin.read())
for path in p['forbidden']:
    try:
        open(path, 'rb')
    except PermissionError:
        pass
    else:
        raise AssertionError('frontend could read private root/bootstrap')
client = CredentialClient(p['path'], server_uid=p['uid'])
item = client.create(Credential('token', p['token']), project_id=UUID(p['project']),
                     name=p['name'], kind='variable', purpose='project', value=b'installed-private-644')
assert item.revision == 1
assert not any(n.startswith(('django', 'mop.server', 'nacl')) for n in sys.modules)
'''
        for number in range(2):
            process = subprocess.Popen(argv, cwd=state, env=env, user=service.pw_uid, group=service.pw_gid,
                extra_groups=(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 20
                while not socket_path.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                if not socket_path.exists():
                    process.terminate()
                    output, error = process.communicate(timeout=30)
                    self.fail('credential daemon failed startup: ' + error.decode())
                self.assertEqual(socket_path.stat().st_uid, service.pw_uid)
                self.assertEqual(socket_path.stat().st_gid, service.pw_gid)
                self.assertEqual(socket_path.stat().st_mode & 0o777, 0o660)
                payload = {**auth, 'name': 'INSTALLED_' + str(number), 'path': str(socket_path),
                    'uid': service.pw_uid, 'forbidden': [str(bootstrap), str(private / 'root.key'),
                    paths['root_trust'] + '/root.key']}
                frontend = subprocess.run([sys.executable, '-I', '-c', client_code], cwd=base, env=env,
                    user=peer.pw_uid, group=peer.pw_gid, extra_groups=(service.pw_gid,),
                    input=json.dumps(payload).encode(), capture_output=True, timeout=15)
                self.assertEqual(frontend.returncode, 0, frontend.stderr.decode())
                # Root даже с текущим token не входит в точный peer allowlist.
                with self.assertRaises(CredentialError):
                    CredentialClient(str(socket_path), server_uid=service.pw_uid).create(
                        Credential('token', auth['token']), project_id=UUID(auth['project']),
                        name='DENIED_ROOT', kind='variable', purpose='project', value=b'forbidden')
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                try:
                    output, error = process.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise AssertionError('credential daemon did not drain on SIGTERM')
            self.assertEqual(process.returncode, 0, error.decode())
            self.assertFalse(socket_path.exists(), 'owned socket not cleaned on SIGTERM')
            for secret in (self.passwords['credential'].encode(), auth['token'].encode(), root, signing,
                           b'installed-private-644'):
                self.assertFalse(secret in output + error + frontend.stdout + frontend.stderr,
                                 'credential daemon/frontend output exposed private material')
        with FreshPostgreSQL.connect(self, 'maintenance') as db:
            self.assertTrue(installation.verify_database(self.spec, db)['verified'])
            self.assertEqual(db.execute('SELECT count(*) FROM secrets_secret').fetchone(), (2,))

    def cleanup_database(self):
        from _installation_cases import FreshPostgreSQL
        try:
            FreshPostgreSQL.cleanup_database(self)
        finally:
            self.admin.close()


# Точка входа -- tests/_credential_uid_fixture.py: gate всегда идёт с
# проверкой реальной пары UID под root, а не запуском модуля наугад.
