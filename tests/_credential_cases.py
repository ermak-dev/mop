"""Изолированный credential use-case socket и настоящий PostgreSQL.

HYPOTHESIS: ordinary runtime не получает root, но maintenance-only factory
не даёт запустить отдельную credential службу. Crypto RPC был бы decrypt oracle.
Проверяем не helper: приватный Unix socket, штатные Access credentials, ORM AEAD,
отзыв во время provider wait, конечный протокол и default-deny delivery.
SOLUTION: отдельная credential-композиция, конечный socket и rootless client;
реальные Access/SecretStore/Grants.read_bundle, default-deny assignment boundary.
RED: 11 missing-service FAIL на PG16/17; затем PermissionError not raised для
выдачи без authorizer, installation scope и изменённого root trust; потерянная
безопасная причина restore fence воспроизведена отдельным behavioral FAIL.
RESULT: PG16 — 21/21, в том числе отдельный daemon с restricted DB login,
stdlib frontend process и сохранённый owned task после timeout. Не live pool.
STATUS: FIXED — see #637; реальные #639 assignment bindings не подменены fixture.
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection
from django.test import TransactionTestCase, override_settings


class CredentialSocketCases(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('mop.server.controller.credentials'),
                             'isolated credential application service is missing')
        from mop.server.controller import credentials as service
        from mop.client.credentials import CredentialClient
        from mop.common.credentials import Credential, CredentialError
        from mop.server.access import models as m, services as access
        from mop.server.catalog.models import Project
        from mop.server.controller.runtime import initialize
        from django.conf import settings
        initialize()
        self.service, self.Client, self.Auth, self.Error = service, CredentialClient, Credential, CredentialError
        self.access = access
        self.user = get_user_model().objects.create_user('credential-user', password='test-password-only')
        source = m.IdentitySource.objects.create(name='local', kind='local', priority=0)
        m.Principal.objects.create(user=self.user, kind='human')
        m.LoginIdentity.objects.create(user=self.user, source=source, external_subject='credential-user')
        self.project = Project.objects.create(name='socket-project', origin='https://example.invalid/socket-project.git')
        m.ProjectMembership.objects.create(user=self.user, source=source, project=self.project)
        for code in ('manage_project_secret', 'view_secret_metadata'):
            self.user.user_permissions.add(Permission.objects.get(content_type__app_label='secrets', codename=code))
        self.token = access.issue_token(self.user.pk)
        self.auth = Credential('token', self.token)
        self.directory = tempfile.TemporaryDirectory(prefix='mop-credential-')
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'credential.sock')
        self.bootstrap = {**settings.BOOTSTRAP, 'role': 'credential'}

    async def exercise(self, body, *, application, peers=None, timeout=2):
        from mop.server.controller.runtime import Runtime
        runtime = Runtime(database_limit=2, queue_limit=0)
        # Composition создаётся до event loop; I/O adapter не является DB snapshot.
        server = self.service.CredentialService(self.path, application=application,
            runtime=runtime, allowed_uids=(os.geteuid(),) if peers is None else peers, timeout=timeout)
        await server.start()
        try:
            client = self.Client(self.path, server_uid=os.geteuid(), timeout=3)
            await body(client, server)
        finally:
            await server.aclose()
            await runtime.aclose()
        self.assertFalse(Path(self.path).exists())
        self.assertEqual(runtime.pending, 0)

    def run_socket(self, body, *, provider=None, **kwargs):
        with override_settings(BOOTSTRAP=self.bootstrap):
            app = self.service.application(provider=provider)
            async_to_sync(self.exercise)(body, application=app, **kwargs)

    def create(self, client, auth=None, name='TOKEN'):
        return client.create(auth or self.auth, project_id=self.project.id, name=name,
            kind='variable', purpose='project', value=b'synthetic-private-value')

    def test_rootless_client_creates_and_stages_encrypted_relational_records(self):
        from mop.server.secrets.models import SecretVersion
        async def body(client, server):
            item = await asyncio.to_thread(self.create, client)
            version = await asyncio.to_thread(client.stage, self.auth, item.id, b'next-private', expected_revision=item.revision)
            self.assertEqual(version.number, 2)
            self.assertNotIn('synthetic-private-value', repr(item))
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
            self.item = item
        self.run_socket(body)
        rows = list(SecretVersion.objects.filter(secret_id=self.item.id))
        self.assertEqual(len(rows), 2)
        self.assertNotIn(b'synthetic-private-value', bytes(rows[0].envelope))
        self.assertNotIn(b'next-private', bytes(rows[1].envelope))

    def test_project_secret_permission_is_not_installation_credential_authority(self):
        async def body(client, server):
            with self.assertRaisesRegex(self.Error, 'installation credential.*not bound'):
                await asyncio.to_thread(client.create, self.auth, project_id=None, name='NOMAD_TOKEN',
                    kind='credential', purpose='nomad', value=b'never-install')
        self.run_socket(body)

    def test_revoked_token_and_missing_permission_cannot_create(self):
        async def body(client, server):
            with self.assertRaises(self.Error):
                await asyncio.to_thread(self.create, client, self.Auth('token', '0' * 40))
            await server.runtime.database(self.user.user_permissions.clear)
            with self.assertRaises(self.Error):
                await asyncio.to_thread(self.create, client)
        self.run_socket(body)

    def test_session_uses_current_standard_access_binding_and_logout_revokes(self):
        from django.contrib.sessions.backends.db import SessionStore
        from django.contrib.auth import SESSION_KEY, BACKEND_SESSION_KEY, HASH_SESSION_KEY
        session = SessionStore()
        session[SESSION_KEY] = str(self.user.pk)
        session[BACKEND_SESSION_KEY] = 'mop.server.controller.authbackend.ControllerBackend'
        session[HASH_SESSION_KEY] = self.user.get_session_auth_hash()
        session.save()
        self.access.bind_session(self.user.pk, session.session_key)
        auth = self.Auth('session', session.session_key)
        async def body(client, server):
            await asyncio.to_thread(self.create, client, auth)
            await server.runtime.database(session.delete)
            with self.assertRaises(self.Error):
                await asyncio.to_thread(self.create, client, auth, 'SECOND')
        self.run_socket(body)

    def test_revocation_during_real_aead_wait_is_checked_after_wait(self):
        from mop.server.secrets.crypto import XChaCha20Provider, Keyring
        from mop.server.secrets.models import Secret
        from rest_framework.authtoken.models import Token
        real = XChaCha20Provider(Keyring('fixture', {'fixture': b'k' * 32}))
        class Revoking:
            def encrypt(inner, value, context):
                self.assertFalse(connection.in_atomic_block)
                envelope = real.encrypt(value, context)
                Token.objects.filter(user=self.user).delete()
                return envelope
            def decrypt(inner, envelope, context):
                return real.decrypt(envelope, context)
        async def body(client, server):
            with self.assertRaises(self.Error):
                await asyncio.to_thread(self.create, client)
        self.run_socket(body, provider=Revoking())
        self.assertFalse(Secret.objects.exists())

    def test_missing_assignment_gate_denies_even_permitted_human(self):
        async def body(client, server):
            await asyncio.to_thread(self.create, client)
            with self.assertRaisesRegex(self.Error, 'assignment.*not bound'):
                await asyncio.to_thread(client.issue_addressed, self.auth)
        self.run_socket(body)

    def test_peer_uid_is_default_deny_and_client_verifies_server_uid(self):
        async def body(client, server):
            with self.assertRaises(self.Error):
                await asyncio.to_thread(self.create, client)
        self.run_socket(body, peers=())
        async def wrong_server(client, server):
            impostor = self.Client(self.path, server_uid=os.geteuid() + 10000)
            with self.assertRaises(self.Error):
                await asyncio.to_thread(self.create, impostor)
        self.run_socket(wrong_server)

    def test_finite_protocol_refuses_crypto_oracle_and_caller_context(self):
        async def body(client, server):
            for command, args in (('decrypt', {'ciphertext': 'abcd', 'context': 'attacker'}),
                                  ('create', {'actor_id': self.user.pk}),
                                  ('issue_addressed', {'binding_ids': []})):
                reader, writer = await asyncio.open_unix_connection(self.path)
                request = json.dumps({'version': 1, 'auth': {'kind': 'token', 'value': self.token},
                                      'command': command, 'args': args}).encode()
                writer.write(struct.pack('!I', len(request)) + request)
                await writer.drain()
                size = struct.unpack('!I', await reader.readexactly(4))[0]
                response = json.loads(await reader.readexactly(size))
                self.assertFalse(response['ok'])
                self.assertNotIn(self.token, str(response))
                self.assertNotIn('abcd', str(response))
                writer.close()
                await writer.wait_closed()
        self.run_socket(body)

    def test_partial_frame_timeout_and_oversize_are_closed_without_task_leak(self):
        async def body(client, server):
            for data in (b'\x00', struct.pack('!I', 4 * 1024 * 1024)):
                reader, writer = await asyncio.open_unix_connection(self.path)
                writer.write(data)
                await writer.drain()
                async with asyncio.timeout(1):
                    self.assertEqual(await reader.read(), b'')
                writer.close()
                await writer.wait_closed()
            # Idle/frame deadline остаётся .1; последующий полноценный ORM
            # command получает штатный бюджет, не случайный benchmark в 100 ms.
            server.timeout = 2
            await asyncio.to_thread(self.create, client)
        self.run_socket(body, timeout=.1)

    def test_provider_failure_redacts_raw_exception_and_credential(self):
        class Broken:
            def encrypt(inner, value, context):
                raise RuntimeError(self.token + ' synthetic-private-value')
        async def body(client, server):
            with self.assertRaises(self.Error) as caught:
                await asyncio.to_thread(self.create, client)
            self.assertNotIn(self.token, str(caught.exception))
            self.assertNotIn('synthetic-private-value', str(caught.exception))
        self.run_socket(body, provider=Broken())

    def test_service_never_replaces_existing_socket_or_symlink(self):
        async def body(client, server):
            from mop.server.controller.runtime import Runtime
            other = Runtime()
            try:
                duplicate = self.service.CredentialService(self.path, application=server.application,
                    runtime=other, allowed_uids=(os.geteuid(),))
                with self.assertRaises((RuntimeError, ValueError)):
                    await duplicate.start()
                await asyncio.to_thread(self.create, client)
            finally:
                await other.aclose()
        self.run_socket(body)

    def test_exact_delivery_port_cannot_bypass_secret_authorizer(self):
        import uuid
        from mop.server.secrets import services as s
        from mop.server.secrets.crypto import XChaCha20Provider, Keyring
        from mop.server.catalog.models import Node
        node = Node.objects.create(name='delivery-node', driver='host', ssh_host='node.invalid',
            control_origin='https://node.invalid', lan_address='10.0.0.8', bootstrap_address='boot.invalid')
        class Allow:
            def authorize(inner, actor, action, **refs):
                return None
        provider = XChaCha20Provider(Keyring('test', {'test': b'a' * 32}))
        store = s.SecretStore(provider=provider, authorizer=Allow())
        secret = store.create(None, project_id=self.project.id, name='DELIVERY', kind='variable',
            purpose='project', value=b'must-not-deliver')
        binding = store.bind(None, secret.id, expected_revision=secret.revision, node_id=node.id,
            role='worker', digest='a' * 64, incarnation=uuid.uuid4(), grant_id=uuid.uuid4(), assignment_id=uuid.uuid4())
        class ExactAssignment:
            def resolve(inner, actor):
                return s.DeliverySelection((binding.id,), (('variable', 'DELIVERY'),))
        reader = s.SecretStore(provider=provider, delivery_gate=ExactAssignment())
        with self.assertRaisesRegex(PermissionError, 'authorizer'):
            reader.issue_addressed(None)

    def test_stdlib_frontend_process_uses_socket_without_bootstrap_or_server_imports(self):
        import sys
        code = '''
import json, sys
sys.path.insert(0, sys.argv[1])
from mop.client.credentials import CredentialClient
from mop.common.credentials import Credential
payload = json.loads(sys.stdin.read())
from uuid import UUID
client = CredentialClient(payload['path'], server_uid=payload['uid'])
item = client.create(Credential('token', payload['token']), project_id=UUID(payload['project']),
    name='CHILD', kind='variable', purpose='project', value=b'child-private')
assert not any(name.startswith(('mop.server', 'django', 'nacl')) for name in sys.modules)
print(json.dumps({'id': str(item.id), 'revision': item.revision}))
'''
        async def body(client, server):
            process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', code,
                str(Path(__file__).resolve().parents[1]), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={key: value for key, value in os.environ.items() if not key.startswith(('MOP_', 'DJANGO_'))})
            data = json.dumps({'path': self.path, 'uid': os.geteuid(), 'token': self.token,
                               'project': str(self.project.id)}).encode()
            async with asyncio.timeout(5):
                output, error = await process.communicate(data)
            self.assertEqual(process.returncode, 0, error.decode())
            self.assertEqual(json.loads(output)['revision'], 1)
            self.assertNotIn(self.token.encode(), output + error)
            self.assertNotIn(b'child-private', output + error)
        self.run_socket(body)

    def test_retained_bundle_read_is_an_actual_access_use_case_not_raw_decrypt(self):
        import uuid
        with override_settings(BOOTSTRAP=self.bootstrap):
            app = self.service.application()
            actor = self.access.token_context(self.token)
            for code in ('approve_role', 'view_role'):
                self.user.user_permissions.add(Permission.objects.get(content_type__app_label='access', codename=code))
            # Ровно тот же public publish/read, что использует Access, без export ciphertext.
            manifest = b'{"version":1,"soul":"worker","runtime":{"driver":"any","receives_dispatch":true},"capabilities":{"node_verbs":[],"cluster_verbs":[],"spawn":[],"dispatch":false},"prompt":"soul.md"}'
            bundle = app.grants.publish_bundle(actor, project_id=self.project.id, manifest=manifest,
                prompt=b'private prompt', provenance='credential-fixture')
        async def body(client, server):
            result = await asyncio.to_thread(client.read_bundle, self.auth, bundle.id)
            self.assertEqual(result.prompt, b'private prompt')
            self.assertNotIn('private prompt', repr(result))
            self.assertEqual(result.digest, bundle.digest)
            with self.assertRaises(self.Error):
                await asyncio.to_thread(client.read_bundle, self.auth, uuid.uuid4())
        self.run_socket(body)

    def test_composition_checks_runtime_db_privileges_before_reading_root(self):
        with override_settings(BOOTSTRAP=self.bootstrap), \
                patch.object(self.service.bootstrap, 'secret_provider', side_effect=AssertionError('root read before DB gate')):
            with self.assertRaisesRegex(RuntimeError, 'Runtime DB principal'):
                self.service.compose(key_id='root-v1')

    def test_real_commandlet_process_composes_root_with_restricted_db_login(self):
        import uuid
        from psycopg import sql
        role = 'credential_fixture_' + uuid.uuid4().hex
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD %s').format(sql.Identifier(role)), ['fixture-only-password'])
            cursor.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(role)))
            cursor.execute(sql.SQL('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}').format(sql.Identifier(role)))
            cursor.execute(sql.SQL('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}').format(sql.Identifier(role)))
        db = {**self.bootstrap['database'], 'NAME': connection.settings_dict['NAME'],
              'USER': role, 'PASSWORD': 'fixture-only-password'}
        path = Path(self.directory.name) / 'bootstrap.json'
        path.write_text(json.dumps({**self.bootstrap, 'database': db}))
        path.chmod(0o600)
        async def run():
            command = Path(__file__).resolve().parents[1] / 'bin/mop'
            process = await asyncio.create_subprocess_exec(str(command), 'server', 'credentials',
                '--bootstrap', str(path), '--socket', self.path, '--key-id', 'root-v1',
                '--peer-uid', str(os.geteuid()), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd='/', env={key: value for key, value in os.environ.items() if not key.startswith(('MOP_', 'DJANGO_'))})
            try:
                async with asyncio.timeout(8):
                    while not Path(self.path).exists() and process.returncode is None:
                        await asyncio.sleep(.01)
                if process.returncode is not None:
                    output, error = await process.communicate()
                    self.fail('credential daemon startup failed: ' + error.decode())
                client = self.Client(self.path, server_uid=os.geteuid())
                item = await asyncio.to_thread(self.create, client)
                self.assertEqual(item.revision, 1)
                self.assertEqual(process.returncode, None)
            finally:
                if process.returncode is None:
                    process.terminate()
                try:
                    async with asyncio.timeout(12):
                        output, error = await process.communicate()
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    raise
                self.assertEqual(process.returncode, 0, error.decode())
                self.assertNotIn(self.token.encode(), output + error)
                self.assertNotIn(b'synthetic-private-value', output + error)
        try:
            async_to_sync(run)()
            self.assertFalse(Path(self.path).exists())
        finally:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                cursor.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))

    def test_timed_out_crypto_stays_owned_until_real_completion(self):
        import threading
        from mop.server.secrets.crypto import XChaCha20Provider, Keyring
        real = XChaCha20Provider(Keyring('slow', {'slow': b'q' * 32}))
        entered, release = threading.Event(), threading.Event()
        class Slow:
            def encrypt(inner, value, context):
                self.assertFalse(connection.in_atomic_block)
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('bounded test provider wait')
                return real.encrypt(value, context)
        async def body(client, server):
            caller = asyncio.create_task(asyncio.to_thread(self.create, client))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                with self.assertRaises(self.Error):
                    await caller
                self.assertGreater(server.runtime.pending, 0)
                with self.assertRaisesRegex(RuntimeError, 'shutdown incomplete'):
                    await server.runtime.aclose(timeout=.02)
            finally:
                release.set()
                await asyncio.gather(caller, return_exceptions=True)
        self.run_socket(body, provider=Slow(), timeout=.5)

    def test_credential_foundation_check_rejects_wrong_root(self):
        from mop.server.controller.runtime import verify_installation
        root = Path(self.directory.name) / 'wrong-root'
        root.write_bytes(b'another-test-only-root' * 4)
        root.chmod(0o600)
        with override_settings(BOOTSTRAP={**self.bootstrap, 'root_key': str(root)}):
            with self.assertRaisesRegex(RuntimeError, 'root key'):
                verify_installation()

    def test_changed_root_trust_during_crypto_cannot_commit_old_provider_result(self):
        from mop.server.controller.models import Installation
        from mop.server.secrets.models import Secret
        from mop.server.secrets.crypto import XChaCha20Provider, Keyring
        real = XChaCha20Provider(Keyring('old', {'old': b'q' * 32}))
        class Rekeying:
            def encrypt(inner, value, context):
                self.assertFalse(connection.in_atomic_block)
                envelope = real.encrypt(value, context)
                Installation.objects.filter(pk=1).update(root_digest='0' * 64)
                return envelope
        async def body(client, server):
            with self.assertRaises(self.Error):
                await asyncio.to_thread(self.create, client)
        self.run_socket(body, provider=Rekeying())
        self.assertFalse(Secret.objects.exists())

    def test_restore_fence_refusal_preserves_safe_reason(self):
        from mop.server.controller.models import RestoreGuard
        async def body(client, server):
            await server.runtime.database(lambda: RestoreGuard.objects.filter(pk=1).update(state='restoring'))
            with self.assertRaisesRegex(self.Error, 'restore guard is not ready'):
                await asyncio.to_thread(self.create, client)
        self.run_socket(body)

    def test_runtime_role_cannot_enter_maintenance_or_export_provider(self):
        from mop.server.controller.runtime import maintenance, validate_asgi_startup
        with override_settings(BOOTSTRAP=self.bootstrap):
            with self.assertRaisesRegex(RuntimeError, 'maintenance'):
                maintenance('migrate')
            with self.assertRaisesRegex(RuntimeError, 'web/gateway'):
                validate_asgi_startup()
