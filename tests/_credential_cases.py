"""Изолированный credential use-case socket и настоящий PostgreSQL.

HYPOTHESIS: ordinary runtime не получает root, но maintenance-only factory
не даёт запустить отдельную credential службу. Crypto RPC был бы decrypt oracle.
Проверяем не helper: приватный Unix socket, штатные Access credentials, ORM AEAD,
отзыв во время provider wait, конечный протокол и default-deny delivery.
SOLUTION: отдельная credential-композиция, конечный socket и rootless client;
реальные Access/SecretStore/Grants.read_definition, default-deny assignment boundary.
RED: 11 missing-service FAIL на PG16/17; затем PermissionError not raised для
выдачи без authorizer, installation scope и изменённого root trust; потерянная
безопасная причина restore fence воспроизведена отдельным behavioral FAIL.
RESULT: PG16/17 — 23/23, в том числе отдельный daemon с restricted DB login,
stdlib frontend process и сохранённый owned task после timeout. Combined PG17
до двух cleanup checks — 232/232. Дополнительный lifecycle red: main-thread
checkout переживал compose, исчезнувший endpoint ломал shutdown; close_pools
на границе startup и inode-aware cleanup устраняют оба отказа. Не live pool.
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
        from mop.common.credential_client import CredentialClient
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

    def test_bound_assignment_gate_still_denies_even_permitted_human(self):
        from uuid import uuid4
        from mop.server.controller.models import Installation
        Installation.objects.filter(pk=1).update(effects_enabled=True)
        async def body(client, server):
            await asyncio.to_thread(self.create, client)
            with self.assertRaisesRegex(self.Error, 'enrolled node service required'):
                await asyncio.to_thread(client.issue_addressed, self.auth, target_id=uuid4())
        self.run_socket(body)

    # test_distinct_frontend_uid_uses_only_group_protected_socket перенесён
    # без изменения утверждений в узкий root gate #674
    # (_credential_uid_gate.CredentialSocketUids): общий непривилегированный
    # прогон не может его проверить, и молчаливый пропуск читался зелёным.

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

    def test_grant_callback_in_sql_uses_public_local_authority_not_crypto_chain(self):
        from dataclasses import replace
        import uuid
        from django.db import transaction
        # #684: делегирование -- grant_id при живом human credential;
        # подделка вида у чужого credential закрыта по умолчанию.
        actor = replace(self.access.token_context(self.token),
            kind=type(self.access.token_context(self.token).kind).SERVICE, grant_id=uuid.uuid4())
        with override_settings(BOOTSTRAP=self.bootstrap):
            app = self.service.application()
        with transaction.atomic():
            with self.assertRaises(self.access.NotVisible):
                self.service.CurrentAccess(app).authorize(actor, 'secrets.manage_project_secret',
                    project_ids=(self.project.id,))

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
from mop.common.credential_client import CredentialClient
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

    # HYPOTHESIS: rootless web не может publish exact role bytes без своего provider.
    # SOLUTION: отдельный typed publish_definition handler вызывает owner Grants целиком.
    # RESULT: socket дал missing publish_definition; затем exact bytes roundtrip green.
    # Дополнительный red: смена root оставляла bundle после отказа; local grant
    # callback входил в crypto chain под atomic. Оба исправлены, PG16 socket green.
    # STATUS: FIXED — see #669.
    def test_rootless_publish_definition_retains_exact_bytes_and_rechecks_authority(self):
        from mop.server.access.models import AgentDefinitionVersion
        for code in ('approve_role', 'view_role'):
            self.user.user_permissions.add(Permission.objects.get(content_type__app_label='access', codename=code))
        manifest = b'{"version":1,"soul":"worker","runtime":{"driver":"any","receives_dispatch":true},"capabilities":{"node_verbs":[],"cluster_verbs":[],"spawn":[],"dispatch":false},"prompt":"soul.md"}'
        async def body(client, server):
            result = await asyncio.to_thread(client.publish_definition, self.auth, project_id=self.project.id,
                manifest=manifest, prompt=b'rootless private prompt', provenance='rootless-fixture')
            self.assertEqual(result.project_id, self.project.id)
            self.assertEqual(result.owner_id, self.user.pk)
            content = await asyncio.to_thread(client.read_definition, self.auth, result.id)
            self.assertEqual(content.manifest, manifest)
            self.assertEqual(content.prompt, b'rootless private prompt')
            self.assertEqual(content.digest, result.digest)
            await server.runtime.database(self.user.user_permissions.clear)
            with self.assertRaises(self.Error):
                await asyncio.to_thread(client.publish_definition, self.auth, project_id=self.project.id,
                    manifest=manifest, prompt=b'new denied prompt', provenance='rootless-fixture')
        self.run_socket(body)
        self.assertEqual(AgentDefinitionVersion.objects.count(), 1)

    def test_bundle_publication_cannot_commit_after_root_changes_during_crypto(self):
        from mop.server.controller.models import Installation
        from mop.server.access.models import AgentDefinitionVersion
        from mop.server.secrets.crypto import XChaCha20Provider, Keyring
        real = XChaCha20Provider(Keyring('old', {'old': b'q' * 32}))
        for code in ('approve_role', 'view_role'):
            self.user.user_permissions.add(Permission.objects.get(content_type__app_label='access', codename=code))
        class Rekeying:
            def encrypt(inner, value, context):
                self.assertFalse(connection.in_atomic_block)
                encrypted = real.encrypt(value, context)
                Installation.objects.filter(pk=1).update(root_digest='0' * 64)
                return encrypted
        manifest = b'{"version":1,"soul":"worker","runtime":{"driver":"any","receives_dispatch":true},"capabilities":{"node_verbs":[],"cluster_verbs":[],"spawn":[],"dispatch":false},"prompt":"soul.md"}'
        async def body(client, server):
            with self.assertRaises(self.Error):
                await asyncio.to_thread(client.publish_definition, self.auth, project_id=self.project.id,
                    manifest=manifest, prompt=b'must not persist', provenance='fixture')
        self.run_socket(body, provider=Rekeying())
        self.assertFalse(AgentDefinitionVersion.objects.exists(), 'root change was detected only after aggregate commit')

    # HYPOTHESIS: web без root не может сохранить exact Preparation bytes или
    # выдать intent с настоящим approval; чужой owner в JSON не является proof.
    # SOLUTION: finite aggregate commands, Preparation.approval_query и live Access.
    # RED: PG16 missing publish_preparation; PG17 safe grant conflict был заменён
    # generic unknown. Fixture projection live ORM исправлена отдельно, не red.
    # RESULT: PG17 — 29 socket checks green, включая actual distinct UID.
    # STATUS: FIXED — see #669 (stage: bytes/approval/reservation; binding всё ещё deny).
    # HYPOTHESIS: unbound BindingQuery блокирует provision при реальных owner APIs.
    # SOLUTION: Root использует SQL-only Assignments.current и полный RoleBinding;
    # activation/retirement/addressed proof не выводятся из простой projection.
    # RESULT: actual PG16 — два отказа runtime binding query is not bound до fix;
    # затем provision/current прошли, старый stage-only assertion обновлён под
    # новый контракт; mismatch до/после crypto проверяется без token/commit.
    # STATUS: FIXED — see #669 (current/provision, не delivery acceptance).
    def test_public_assignment_query_maps_complete_binding_inside_sql(self):
        from datetime import timedelta
        import uuid
        from django.db import transaction
        from django.utils import timezone
        from mop.server.catalog.models import Node, NodeProject
        from mop.server.sessions.models import Assignment, ExecutionIncarnation
        from mop.server.access.delegation import RoleBinding
        node = Node.objects.create(name='binding-node', driver='host', ssh_host='node.invalid',
            control_origin='https://node.invalid', lan_address='10.0.0.8', bootstrap_address='boot.invalid')
        NodeProject.objects.create(node=node, project=self.project)
        grant_id = uuid.uuid4()
        assignment = Assignment.objects.create(project=self.project, node=node, owner=self.user,
            role='worker', bundle_digest='a' * 64, grant_id=grant_id,
            expires_at=timezone.now() + timedelta(hours=1))
        incarnation = ExecutionIncarnation.objects.create(assignment=assignment, node=node, executor_epoch=uuid.uuid4())
        assignment.current_incarnation = incarnation
        assignment.save(update_fields=['current_incarnation'])
        with override_settings(BOOTSTRAP=self.bootstrap):
            app = self.service.application()
        with transaction.atomic():
            actual = app.grants.bindings.current(self.project.id, assignment.id)
            self.assertEqual(actual, RoleBinding(self.project.id, assignment.id, incarnation.id,
                'worker', 'a' * 64, grant_id, self.user.pk))
            self.assertIsNone(app.grants.bindings.current(uuid.uuid4(), assignment.id))
            # A read projection не является подтверждением activation/retirement.
            # #643: квитанция активации связана -- но только подтверждёнными
            # identity и готовностью текущей инкарнации; одной проекции её нет.
            self.assertIsNone(app.grants.bindings.activation(grant_id))
            with self.assertRaises(PermissionError):
                app.grants.bindings.retirement(grant_id, 1)

    def reserved_grant(self, *, secret_names=()):
        from datetime import timedelta
        import uuid
        from django.utils import timezone
        for action in ('access.approve_role', 'access.view_role', 'access.issue_grant',
                       'execution.create_puppet', 'preparation.approve_bootstrap'):
            app_label, code = action.split('.')
            self.user.user_permissions.add(Permission.objects.get(content_type__app_label=app_label, codename=code))
        with override_settings(BOOTSTRAP=self.bootstrap):
            app = self.service.application()
        actor = self.access.token_context(self.token)
        revision = app.preparation.publish(actor, project_id=self.project.id, tasks=b'tasks', variables=b'',
            provenance='fixture', commit='a' * 40)
        app.preparation.approve(actor, revision.id)
        manifest = b'{"version":1,"soul":"worker","runtime":{"driver":"any","receives_dispatch":true},"capabilities":{"node_verbs":[],"cluster_verbs":[],"spawn":[],"dispatch":false},"prompt":"soul.md"}'
        if secret_names:
            document = json.loads(manifest)
            document['secrets'] = {'vars': list(secret_names), 'files': []}
            manifest = json.dumps(document).encode()
        bundle = app.grants.publish_definition(actor, project_id=self.project.id, manifest=manifest,
            prompt=b'private', provenance='fixture')
        app.grants.approve_definition(actor, bundle.id)
        # #688: субъект — заранее зарегистрированный agent Actor со своим token.
        from _grant_cases import delegation_subject
        profile, _, _ = delegation_subject(app.grants, actor, project_id=self.project.id,
            definition_id=bundle.id)
        return app.grants.reserve(actor, project_id=self.project.id, definition_id=bundle.id,
            target_id=profile.target_id, incarnation=uuid.uuid4(), request_id=uuid.uuid4(),
            expires_at=timezone.now() + timedelta(hours=1))

    def grant_assignment(self, reserved):
        from datetime import timedelta
        from uuid import uuid4
        from django.utils import timezone
        from mop.server.catalog.models import Node, NodeProject
        from mop.server.sessions.models import Assignment, ExecutionIncarnation
        node = Node.objects.create(name='provision-node', driver='host', ssh_host='node.invalid',
            control_origin='https://node.invalid', lan_address='10.0.0.8', bootstrap_address='boot.invalid')
        NodeProject.objects.create(node=node, project=self.project)
        assignment = Assignment.objects.create(id=reserved.target_id, project=self.project, node=node,
            owner=self.user, role=reserved.role, bundle_digest=reserved.digest, grant_id=reserved.id,
            expires_at=timezone.now() + timedelta(hours=1))
        incarnation = ExecutionIncarnation.objects.create(id=reserved.incarnation, assignment=assignment,
            node=node, executor_epoch=uuid4())
        assignment.current_incarnation = incarnation
        assignment.save(update_fields=['current_incarnation'])
        return assignment

    def test_real_assignment_query_allows_rootless_provision_without_token_export(self):
        from mop.server.access.models import Grant
        from mop.server.secrets.models import SecretVersion
        from rest_framework.authtoken.models import Token
        reserved = self.reserved_grant()
        self.grant_assignment(reserved)
        async def body(client, server):
            result = await asyncio.to_thread(client.provision_grant, self.auth, reserved.id,
                expected_revision=reserved.revision)
            self.assertEqual(result.revision, reserved.revision + 1)
            self.assertIsNotNone(result.credential_version_id)
            self.result = result
        self.run_socket(body)
        grant = Grant.objects.get(pk=reserved.id)
        # #688: retained value — транспортный credential делегирования; общий
        # token субъекта в него и в ответ не попадает.
        token = Token.objects.get(user_id=grant.subject_id)
        version = SecretVersion.objects.get(pk=self.result.credential_version_id)
        self.assertNotIn(token.key, repr(self.result))
        self.assertNotIn(token.key.encode(), bytes(version.envelope))

    def test_existing_mismatched_assignment_refuses_provision_before_crypto(self):
        from uuid import uuid4
        from mop.server.access.models import Grant
        from rest_framework.authtoken.models import Token
        reserved = self.reserved_grant()
        subject_tokens = list(Token.objects.filter(user_id=reserved.subject_id).values_list('key', flat=True))
        assignment = self.grant_assignment(reserved)
        assignment.grant_id = uuid4()
        assignment.save(update_fields=['grant_id'])
        class NoCrypto:
            def encrypt(inner, value, context):
                self.fail('mismatched assignment must be refused before provider work')
        async def body(client, server):
            with self.assertRaisesRegex(self.Error, 'provisioning binding changed'):
                await asyncio.to_thread(client.provision_grant, self.auth, reserved.id,
                    expected_revision=reserved.revision)
        self.run_socket(body, provider=NoCrypto())
        row = Grant.objects.get(pk=reserved.id)
        self.assertEqual(row.revision, reserved.revision)
        self.assertIsNone(row.credential_version_id)
        # #688: provision не выдаёт token; общий token субъекта не тронут.
        self.assertEqual(list(Token.objects.filter(user_id=row.subject_id).values_list('key', flat=True)),
                         subject_tokens)

    def test_assignment_rebinding_during_crypto_prevents_retained_commit_and_token(self):
        from uuid import uuid4
        from mop.server.access.models import Grant
        from mop.server.sessions.models import Assignment
        from mop.server.secrets.models import SecretVersion
        from mop.server.secrets.crypto import XChaCha20Provider, Keyring
        from rest_framework.authtoken.models import Token
        reserved = self.reserved_grant()
        subject_tokens = list(Token.objects.filter(user_id=reserved.subject_id).values_list('key', flat=True))
        assignment = self.grant_assignment(reserved)
        before = SecretVersion.objects.count()
        real = XChaCha20Provider(Keyring('fixture', {'fixture': b'k' * 32}))
        class Rebinding:
            def encrypt(inner, value, context):
                self.assertFalse(connection.in_atomic_block)
                envelope = real.encrypt(value, context)
                Assignment.objects.filter(pk=assignment.id).update(grant_id=uuid4())
                return envelope
        async def body(client, server):
            with self.assertRaisesRegex(self.Error, 'provisioning binding changed'):
                await asyncio.to_thread(client.provision_grant, self.auth, reserved.id,
                    expected_revision=reserved.revision)
        self.run_socket(body, provider=Rebinding())
        row = Grant.objects.get(pk=reserved.id)
        self.assertEqual(row.revision, reserved.revision)
        self.assertIsNone(row.credential_version_id)
        self.assertEqual(SecretVersion.objects.count(), before)
        # #688: provision не выдаёт token; общий token субъекта не тронут.
        self.assertEqual(list(Token.objects.filter(user_id=row.subject_id).values_list('key', flat=True)),
                         subject_tokens)

    def test_rootless_preparation_bytes_and_real_policy_backed_grant_reservation(self):
        from datetime import timedelta
        import uuid
        from django.utils import timezone
        from mop.server.preparation.models import BootstrapRevision
        for action in ('access.approve_role', 'access.view_role', 'access.issue_grant',
                       'execution.create_puppet', 'preparation.approve_bootstrap'):
            app_label, code = action.split('.')
            self.user.user_permissions.add(Permission.objects.get(content_type__app_label=app_label, codename=code))
        manifest = b'{"version":1,"soul":"worker","runtime":{"driver":"any","receives_dispatch":true},"capabilities":{"node_verbs":[],"cluster_verbs":[],"spawn":[],"dispatch":false},"prompt":"soul.md"}'
        async def body(client, server):
            revision = await asyncio.to_thread(client.publish_preparation, self.auth,
                project_id=self.project.id, tasks=b'- debug: msg=hello\r\n', variables=b'private: value\n',
                provenance='git:fixture', commit='a' * 40, secret_refs=())
            content = await asyncio.to_thread(client.read_preparation, self.auth, revision.id)
            self.assertEqual(content.tasks, b'- debug: msg=hello\r\n')
            self.assertEqual(content.variables, b'private: value\n')
            self.assertNotIn('private: value', repr(content))
            approval = await asyncio.to_thread(client.approve_preparation, self.auth, revision.id)
            bundle = await asyncio.to_thread(client.publish_definition, self.auth, project_id=self.project.id,
                manifest=manifest, prompt=b'private role prompt', provenance='fixture')
            await asyncio.to_thread(client.approve_definition, self.auth, bundle.id)
            # #688: субъект — заранее зарегистрированный тем же owner agent
            # Actor; credential wire его не создаёт.
            def register():
                from _grant_cases import delegation_subject
                from mop.server.access.grants import Grants
                # register_agent не трогает secrets: provider здесь не нужен.
                profile, _, _ = delegation_subject(Grants(provider=None),
                    self.access.token_context(self.token), project_id=self.project.id,
                    definition_id=bundle.id)
                return profile.target_id
            target_id, incarnation = await server.runtime.database(register), uuid.uuid4()
            reserved = await asyncio.to_thread(client.reserve_grant, self.auth, project_id=self.project.id,
                definition_id=bundle.id, target_id=target_id, incarnation=incarnation, request_id=uuid.uuid4(),
                expires_at=timezone.now() + timedelta(hours=1))
            self.assertEqual((reserved.project_id, reserved.target_id, reserved.incarnation),
                (self.project.id, target_id, incarnation))
            self.assertEqual(reserved.state, 'reserved')
            with self.assertRaisesRegex(self.Error, 'grant revision changed'):
                await asyncio.to_thread(client.provision_grant, self.auth, reserved.id,
                    expected_revision=reserved.revision + 1)
            # Pre-registration provisioning разрешён owner-контрактом, когда
            # реальный public Assignment query ещё не находит новый target.
            provisioned = await asyncio.to_thread(client.provision_grant, self.auth, reserved.id,
                expected_revision=reserved.revision)
            self.assertEqual(provisioned.revision, reserved.revision + 1)
            self.assertEqual(provisioned.state, 'reserved')
            row = await server.runtime.database(lambda: BootstrapRevision.objects.values(
                'content_version_id', 'digest').get(pk=revision.id))
            from mop.server.access.models import Grant
            proof = await server.runtime.database(lambda: Grant.objects.values('intent__policy_approval_id',
                'intent__policy_version_id', 'intent__policy_digest', 'credential_version_id').get(pk=reserved.id))
            self.assertEqual(proof['intent__policy_approval_id'], approval.id)
            self.assertEqual(proof['intent__policy_version_id'], row['content_version_id'])
            self.assertEqual(proof['intent__policy_digest'], row['digest'])
            self.assertIsNotNone(proof['credential_version_id'])
            self.assertEqual(proof['credential_version_id'], provisioned.credential_version_id)
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
            bundle = app.grants.publish_definition(actor, project_id=self.project.id, manifest=manifest,
                prompt=b'private prompt', provenance='credential-fixture')
        async def body(client, server):
            result = await asyncio.to_thread(client.read_definition, self.auth, bundle.id)
            self.assertEqual(result.prompt, b'private prompt')
            self.assertNotIn('private prompt', repr(result))
            self.assertEqual(result.digest, bundle.digest)
            with self.assertRaises(self.Error):
                await asyncio.to_thread(client.read_definition, self.auth, uuid.uuid4())
        self.run_socket(body)

    def test_composition_checks_runtime_db_privileges_before_provider_construction(self):
        with override_settings(BOOTSTRAP=self.bootstrap), \
                patch.object(self.service.bootstrap, 'secret_provider', side_effect=AssertionError('provider constructed before DB gate')):
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

    def test_composition_releases_startup_connection_before_async_runtime(self):
        # Этот test отделяет cleanup от elevated fixture login; настоящий
        # restricted DB startup проверен отдельным daemon-process test выше.
        with override_settings(BOOTSTRAP=self.bootstrap), patch.object(self.service, 'maintenance'):
            self.service.compose(key_id='root-v1')
            self.assertIsNone(connection.connection, 'startup held a runtime pool checkout')

    def test_shutdown_tolerates_already_removed_owned_socket(self):
        async def body(client, server):
            await asyncio.to_thread(self.create, client)
            Path(self.path).unlink()
        self.run_socket(body)

    def test_privileged_derivation_does_not_admit_maintenance_to_listener_startup(self):
        for role in ('maintenance', 'web', 'worker', 'gateway', 'node'):
            with self.subTest(role=role), override_settings(BOOTSTRAP={**self.bootstrap, 'role': role}), \
                    patch.object(self.service.bootstrap, 'secret_provider', side_effect=AssertionError('provider reached')):
                with self.assertRaisesRegex(RuntimeError, 'separate credential'):
                    self.service.compose(key_id='root-v1')

    def test_runtime_role_cannot_enter_maintenance_or_export_provider(self):
        from mop.server.controller.runtime import maintenance, validate_asgi_startup
        with override_settings(BOOTSTRAP=self.bootstrap):
            with self.assertRaisesRegex(RuntimeError, 'maintenance'):
                maintenance('migrate')
            with self.assertRaisesRegex(RuntimeError, 'web/gateway'):
                validate_asgi_startup()


# HYPOTHESIS: activate существует лишь как trusted Python port, а worker без
# root/human token не может завершить create. Generic activate-any-grant опасен.
# SPEC: permission access.activate_grant + node-bound grant-activator enrollment;
# только точный claimed create, успешные стадии, current identity/readiness.
# Fixture засевает persisted стадии/receipts, а не выдаёт их за запуск scheduler.
# Provider, Preparation approvals, Grants, Actor/RBAC и Unix socket настоящие.
# RED: PG17 -- unsupported credential command вместо authorization refusal;
# canonical permission отсутствует. Дополнительные red: явный отказ не был
# PermissionError для worker, human с enrollment активировал грант.
# SOLUTION: finite activate_grant, canonical RBAC permission + node-bound
# grant-activator enrollment и request-local claimed-create authority; все
# проверки повторяются после provider wait и в финальном callback Grants.
# RESULT: PG17 combined credential/enrollment/RBAC/schema -- 149/149; два
# inherited cross-UID checks -- 2/2. Actual socket workflow и потерянный ответ,
# stdlib worker без human credential, AEAD corruption/approval/revocation -- green.
# STATUS: FIXED — see #643 (без production startup enrollment/worker wiring).
class ActivationCredentialCases(TransactionTestCase):
    serialized_rollback = True
    setUp = CredentialSocketCases.setUp
    exercise = CredentialSocketCases.exercise
    run_socket = CredentialSocketCases.run_socket
    reserved_grant = CredentialSocketCases.reserved_grant
    grant_assignment = CredentialSocketCases.grant_assignment

    async def raw(self, auth, grant_id, revision):
        from mop.common import credentials as p
        reader, writer = await asyncio.open_unix_connection(self.path)
        try:
            writer.write(p.frame({'version': 1, 'auth': {'kind': auth.kind, 'value': auth.value},
                'command': 'activate_grant', 'args': {'grant_id': str(grant_id), 'expected_revision': revision}}))
            await writer.drain()
            size = p.frame_size(await reader.readexactly(4))
            return p.loads(await reader.readexactly(size))
        finally:
            writer.close()
            await writer.wait_closed()

    def test_human_and_unrelated_service_are_not_activation_authority(self):
        from uuid import uuid4
        from mop.server.catalog.models import Node, NodeProject
        node = Node.objects.create(name='unrelated-executor', driver='host', ssh_host='node.invalid',
            control_origin='https://node.invalid', lan_address='node.invalid', bootstrap_address='node.invalid')
        NodeProject.objects.create(node=node, project=self.project)
        key = self.access.enroll_service('unrelated-executor', node.id, project_ids=(self.project.id,),
            permissions=('execution.poll_work',))
        async def body(client, server):
            for auth in (self.auth, self.Auth('token', key)):
                result = await self.raw(auth, uuid4(), 2)
                self.assertFalse(result['ok'])
                self.assertEqual(result['error']['message'], 'enrolled grant activator required')
                self.assertNotIn(auth.value, str(result))
        self.run_socket(body)

    def fixture(self):
        from datetime import timedelta
        from uuid import uuid4
        from django.contrib.auth import get_user_model
        from django.utils import timezone
        from mop.server.controller.models import Installation
        from mop.server.operations import models as operation
        from mop.server.sessions import models as execution
        from mop.server.controller.models import RestoreGuard
        # Отсутствующее canonical permission -- behavioral failure, не импорт/setup error.
        permission = Permission.objects.filter(content_type__app_label='access', codename='activate_grant').first()
        self.assertIsNotNone(permission, 'canonical activation action permission is missing')
        Installation.objects.filter(pk=1).update(effects_enabled=True)
        reserved = self.reserved_grant()
        self.assignment = self.grant_assignment(reserved)
        with override_settings(BOOTSTRAP=self.bootstrap):
            self.app = self.service.application()
        self.provisioned = self.app.grants.provision(self.access.token_context(self.token), reserved.id,
            expected_revision=reserved.revision)
        self.grant_id = reserved.id
        self.worker_key = self.access.enroll_service('activation-worker', self.assignment.node_id,
            project_ids=(self.project.id,), permissions=('access.activate_grant',), use_case='grant-activator')
        self.worker = get_user_model().objects.get(username='activation-worker')
        self.worker_auth = self.Auth('token', self.worker_key)
        installation, now = Installation.objects.get(pk=1), timezone.now()
        self.operation = operation.Operation.objects.create(actor=self.user, actor_binding={}, request_id=uuid4(),
            digest='a'*64, epoch=installation.epoch, restore_generation=RestoreGuard.objects.get(pk=1).generation,
            action='create', node_id=self.assignment.node_id, target_kind='puppet', target_id=self.assignment.id,
            incarnation_id=reserved.incarnation, expected_revision=self.assignment.revision,
            not_after=now + timedelta(minutes=2), state='running')
        self.operation.projects.set((self.project.id,))
        operation.RestartBinding.objects.create(operation=self.operation, project=self.project,
            executor_epoch=self.assignment.current_incarnation.executor_epoch, owner_id=self.user.pk,
            generation=self.assignment.generation, role=reserved.role, bundle_digest=reserved.digest, grant_id=reserved.id)
        self.steps, self.attempts = [], []
        for ordinal, (action, evidence) in enumerate((('scheduler_register', 'job-registered'),
                ('apply_identity', 'identity-applied'), ('confirm_ready', 'ready'), ('activate_grant', '')), 1):
            token = uuid4()
            step = operation.Step.objects.create(operation=self.operation, ordinal=ordinal, name=action,
                token=token, attempt_id=token, outcome='succeeded' if ordinal < 4 else '', evidence=evidence,
                started_at=now, dispatched_at=now, lease_until=now + timedelta(seconds=30),
                quiescent_at=now if ordinal < 4 else None)
            self.steps.append(step)
            if action in ('apply_identity', 'confirm_ready'):
                # Исполнитель узла отличается от worker активации.
                if len(self.attempts) == 0:
                    machine_key = self.access.enroll_service('identity-executor', self.assignment.node_id,
                        project_ids=(self.project.id,), permissions=('execution.report_result',))
                    self.machine = self.access.token_context(machine_key)
                attempt = execution.AssignmentAttempt.objects.create(assignment=self.assignment,
                    incarnation_id=reserved.incarnation, executor_user_id=self.machine.user_id, request_id=uuid4(),
                    epoch=installation.epoch, operation_id=self.operation.id, step_id=step.id,
                    executor_epoch=self.assignment.current_incarnation.executor_epoch, owner=self.user,
                    generation=self.assignment.generation, assignment_revision=self.assignment.revision,
                    role=reserved.role, bundle_digest=reserved.digest, grant_id=reserved.id, action=action,
                    not_after=self.operation.not_after, lease_until=step.lease_until, state='succeeded', revision=2)
                receipt = execution.ResultReceipt.objects.create(attempt=attempt, expected_revision=1,
                    resulting_revision=2, outcome='succeeded', evidence=evidence)
                model = execution.IdentityApplication if action == 'apply_identity' else execution.ReadinessReceipt
                model.objects.create(attempt=attempt, receipt=receipt)
                self.attempts.append(attempt)
        for key in ('node:'+str(self.assignment.node_id), 'project:'+str(self.project.id),
                    'puppet:'+str(self.assignment.id), 'target:'+str(self.assignment.id)):
            resource = operation.Resource.objects.create(key=key)
            operation.ResourceClaim.objects.create(resource=resource, operation=self.operation,
                step=self.steps[-1], token=self.steps[-1].token)
        return permission

    def activate(self, client, *, auth=None, revision=None):
        return client.activate_grant(auth or self.worker_auth, self.grant_id,
            expected_revision=self.provisioned.revision if revision is None else revision)

    def refused(self, change, *, provider=None, reason=None):
        from mop.server.access.models import Grant, GrantEvent
        self.fixture()
        change()
        async def body(client, server):
            with self.assertRaisesRegex(self.Error, reason or '.'):
                await asyncio.to_thread(self.activate, client)
        self.run_socket(body, provider=provider)
        self.assertEqual(Grant.objects.get(pk=self.grant_id).state, 'reserved')
        self.assertFalse(GrantEvent.objects.filter(grant_id=self.grant_id, action='activate').exists())

    def test_ready_claimed_create_activates_over_socket_without_human_credential_in_worker(self):
        from mop.common.credential_client import GrantActivator
        from mop.server.access.models import Grant, GrantEvent
        self.fixture()
        async def body(client, server):
            adapter = GrantActivator(client, self.worker_auth)
            result = await asyncio.to_thread(adapter.activate_grant, self.grant_id,
                expected_revision=self.provisioned.revision)
            self.assertEqual((result.id, result.state, result.revision),
                (self.grant_id, 'active', self.provisioned.revision + 1))
            self.assertEqual(result.credential_version_id, self.provisioned.credential_version_id)
            for value in (self.token, self.worker_key, 'private'):
                self.assertNotIn(value, repr(result) + repr(adapter))
        self.run_socket(body)
        self.assertEqual(Grant.objects.get(pk=self.grant_id).state, 'active')
        self.assertEqual(GrantEvent.objects.filter(grant_id=self.grant_id, action='activate').count(), 1)

    def test_explicit_activation_refusal_is_not_an_unknown_transport_outcome(self):
        self.fixture()
        self.worker.user_permissions.clear()
        async def body(client, server):
            with self.assertRaises(self.Error) as caught:
                await asyncio.to_thread(self.activate, client)
            self.assertIsInstance(caught.exception, PermissionError,
                'worker must record explicit refusal as failed, not unknown transport outcome')
        self.run_socket(body)

    def test_human_cannot_become_worker_through_an_enrollment_row(self):
        from mop.server.access.models import ServiceEnrollment
        permission = self.fixture()
        self.user.user_permissions.add(permission)
        ServiceEnrollment.objects.create(actor=self.user, node_id=self.assignment.node_id, use_case='grant-activator')
        async def body(client, server):
            with self.assertRaisesRegex(self.Error, 'enrolled grant activator required'):
                await asyncio.to_thread(self.activate, client, auth=self.auth)
        self.run_socket(body)

    def test_node_executor_with_activation_permission_is_not_the_worker_enrollment(self):
        self.fixture()
        key = self.access.enroll_service('not-activation-worker', self.assignment.node_id,
            project_ids=(self.project.id,), permissions=('access.activate_grant',))
        async def body(client, server):
            with self.assertRaisesRegex(self.Error, 'enrolled grant activator required'):
                await asyncio.to_thread(self.activate, client, auth=self.Auth('token', key))
        self.run_socket(body)

    def test_node_free_controller_worker_cannot_activate_even_with_permission(self):
        permission = self.fixture()
        from django.contrib.auth import get_user_model
        key = self.access.enroll_service('unbound-worker', None, project_ids=(),
            permissions=('catalog.record_settings_application',), use_case='controller-worker')
        get_user_model().objects.get(username='unbound-worker').user_permissions.add(permission)
        async def body(client, server):
            with self.assertRaisesRegex(self.Error, 'enrolled grant activator required'):
                await asyncio.to_thread(self.activate, client, auth=self.Auth('token', key))
        self.run_socket(body)

    def test_claim_changes_during_decryption_cannot_commit_activation(self):
        from mop.server.controller import bootstrap
        from mop.server.operations.models import Step
        from mop.server.access.models import Grant
        self.fixture()
        provider = bootstrap.secret_provider(self.bootstrap, installation_id=self.app.snapshot['identity'],
            expected_root_digest=self.app.snapshot['root_digest'], key_id='root-v1')
        class Reclaiming:
            def decrypt(inner, envelope, context):
                from uuid import uuid4
                self.assertFalse(connection.in_atomic_block)
                value = provider.decrypt(envelope, context)
                Step.objects.filter(pk=self.steps[-1].id).update(token=uuid4())
                return value
        async def body(client, server):
            with self.assertRaisesRegex(self.Error, 'claimed create'):
                await asyncio.to_thread(self.activate, client)
        self.run_socket(body, provider=Reclaiming())
        self.assertEqual(Grant.objects.get(pk=self.grant_id).state, 'reserved')

    def test_corrupt_retained_definition_cannot_activate_through_worker_service(self):
        from mop.server.access.models import Grant
        from mop.server.secrets.models import SecretVersion
        def corrupt():
            version_id = Grant.objects.get(pk=self.grant_id).intent.definition.content_version_id
            version = SecretVersion.objects.get(pk=version_id)
            value = bytes(version.envelope)
            SecretVersion._base_manager.filter(pk=version_id).update(envelope=value[:-1] + bytes([value[-1] ^ 1]))
        self.refused(corrupt, reason='authentication failed')

    def test_revoked_bootstrap_approval_cannot_activate_through_worker_service(self):
        from mop.server.access.models import Grant
        from mop.server.preparation.models import ApprovalRevocation
        def revoke():
            grant = Grant.objects.get(pk=self.grant_id)
            ApprovalRevocation.objects.create(approval_id=grant.intent.policy_approval_id)
        self.refused(revoke)

    def test_stdlib_worker_process_never_receives_root_or_original_human_credential(self):
        import sys
        self.fixture()
        code = '''
import json, sys
sys.path.insert(0, sys.argv[1])
from uuid import UUID
from mop.common.credential_client import CredentialClient, GrantActivator
from mop.common.credentials import Credential
value = json.loads(sys.stdin.read())
worker = GrantActivator(CredentialClient(value['path'], server_uid=value['uid']), Credential('token', value['service_token']))
receipt = worker.activate_grant(UUID(value['grant']), expected_revision=value['revision'])
assert not any(name.startswith(('mop.server', 'django', 'nacl')) for name in sys.modules)
print(json.dumps({'state': receipt.state, 'revision': receipt.revision}))
'''
        async def body(client, server):
            process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', code,
                str(Path(__file__).resolve().parents[1]), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={key: value for key, value in os.environ.items() if not key.startswith(('MOP_', 'DJANGO_'))})
            data = json.dumps({'path': self.path, 'uid': os.geteuid(), 'service_token': self.worker_key,
                'grant': str(self.grant_id), 'revision': self.provisioned.revision}).encode()
            self.assertNotIn(self.token.encode(), data)
            async with asyncio.timeout(5):
                output, error = await process.communicate(data)
            self.assertEqual(process.returncode, 0, error.decode())
            self.assertEqual(json.loads(output)['state'], 'active')
            for value in (self.token.encode(), self.worker_key.encode()):
                self.assertNotIn(value, output + error)
        self.run_socket(body)

    def test_revoked_subject_credential_is_still_checked_by_grants(self):
        from rest_framework.authtoken.models import Token
        self.refused(lambda: Token.objects.filter(user_id=self.provisioned.subject_id).delete())

    def test_cas_revision_mismatch_does_not_activate(self):
        self.fixture()
        async def body(client, server):
            with self.assertRaisesRegex(self.Error, 'grant revision changed'):
                await asyncio.to_thread(self.activate, client, revision=self.provisioned.revision + 1)
        self.run_socket(body)

    def test_human_with_activation_permission_still_cannot_activate(self):
        permission = self.fixture()
        self.user.user_permissions.add(permission)
        async def body(client, server):
            with self.assertRaisesRegex(self.Error, 'enrolled grant activator required'):
                await asyncio.to_thread(self.activate, client, auth=self.auth)
        self.run_socket(body)

    def test_missing_activation_permission_refuses(self):
        self.refused(lambda: self.worker.user_permissions.clear(), reason='permission required')

    def test_deleted_enrollment_refuses(self):
        from mop.server.access.models import ServiceEnrollment
        self.refused(lambda: ServiceEnrollment.objects.filter(actor=self.worker).delete())

    def test_revoked_worker_token_refuses(self):
        from rest_framework.authtoken.models import Token
        self.refused(lambda: Token.objects.filter(user=self.worker).delete())

    def test_service_project_scope_removed_refuses(self):
        from mop.server.access.models import ProjectMembership
        self.refused(lambda: ProjectMembership.objects.filter(user=self.worker).delete(), reason='outside enrollment')

    def test_node_project_scope_removed_refuses(self):
        from mop.server.catalog.models import NodeProject
        self.refused(lambda: NodeProject.objects.filter(node_id=self.assignment.node_id).delete(), reason='current create assignment')

    def test_foreign_node_service_with_permission_refuses(self):
        from mop.server.catalog.models import Node, NodeProject
        self.fixture()
        node = Node.objects.create(name='foreign-worker', driver='host', ssh_host='foreign.invalid',
            control_origin='https://foreign.invalid', lan_address='foreign.invalid', bootstrap_address='foreign.invalid')
        NodeProject.objects.create(node=node, project=self.project)
        key = self.access.enroll_service('foreign-worker', node.id, project_ids=(self.project.id,),
            permissions=('access.activate_grant',), use_case='grant-activator')
        async def body(client, server):
            with self.assertRaisesRegex(self.Error, 'outside enrollment'):
                await asyncio.to_thread(self.activate, client, auth=self.Auth('token', key))
        self.run_socket(body)

    def test_no_claimed_create_operation_is_not_generic_grant_activation(self):
        from mop.server.operations.models import Operation
        self.refused(lambda: Operation.objects.filter(pk=self.operation.id).update(state='failed'), reason='claimed create')

    def test_unclaimed_or_completed_activation_step_refuses(self):
        from mop.server.operations.models import Step
        self.refused(lambda: Step.objects.filter(pk=self.steps[-1].id).update(token=None), reason='claimed create')

    def test_unsuccessful_predecessor_refuses(self):
        from mop.server.operations.models import Step
        self.refused(lambda: Step.objects.filter(pk=self.steps[1].id).update(outcome='unknown'), reason='claimed create')

    def test_released_resource_fence_refuses(self):
        from django.utils import timezone
        from mop.server.operations.models import ResourceClaim
        self.refused(lambda: ResourceClaim.objects.filter(operation=self.operation).update(released_at=timezone.now()),
            reason='claimed create')

    def test_actual_readiness_is_required_not_only_successful_workflow_steps(self):
        from mop.server.sessions.models import AssignmentAttempt
        self.refused(lambda: AssignmentAttempt.objects.filter(pk=self.attempts[1].id).update(state='unknown'),
            reason='activation receipt')

    def test_creator_credential_revocation_is_still_checked_by_grants(self):
        from rest_framework.authtoken.models import Token
        self.refused(lambda: Token.objects.filter(user=self.user).delete(), reason='token revoked or rotated')

    def test_service_permission_revoked_during_real_decryption_cannot_commit_activation(self):
        from django.conf import settings
        from mop.server.controller import bootstrap
        self.fixture()
        provider = bootstrap.secret_provider(self.bootstrap, installation_id=self.app.snapshot['identity'],
            expected_root_digest=self.app.snapshot['root_digest'], key_id='root-v1')
        # Реальный provider fixture, не patch результата Grants.activate.
        class Revoking:
            def decrypt(inner, envelope, context):
                self.assertFalse(connection.in_atomic_block)
                result = provider.decrypt(envelope, context)
                self.worker.user_permissions.clear()
                return result
        async def body(client, server):
            with self.assertRaisesRegex(self.Error, 'permission required'):
                await asyncio.to_thread(self.activate, client)
        self.run_socket(body, provider=Revoking())
        from mop.server.access.models import Grant
        self.assertEqual(Grant.objects.get(pk=self.grant_id).state, 'reserved')


class CreateSocketActivationCases(TransactionTestCase):
    """Настоящие admission, node results и GrantActivations используют socket port.

    Scheduler -- owned HTTP fixture, не live pool; криптография и все ORM
    approvals настоящие. Тест не передаёт worker исходный human credential.
    """
    serialized_rollback = True
    exercise = CredentialSocketCases.exercise

    def setUp(self):
        from _create_cases import CreateActivation
        from mop.server.controller import credentials as service
        from mop.common.credential_client import CredentialClient
        from django.conf import settings
        CreateActivation.setUp(self)
        self.service, self.Client = service, CredentialClient
        self.directory = tempfile.TemporaryDirectory(prefix='mop-activation-')
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'credential.sock')
        self.bootstrap = {**settings.BOOTSTRAP, 'role': 'credential'}

    def ready(self, **kwargs):
        from _create_cases import CreateVertical
        from mop.server.preparation.services import Preparation
        CreateVertical.ready(self, **kwargs)
        # Reserve и activation читают реальное approval того же BootstrapRevision.
        from mop.server.access.services import Access
        self.grants.approvals = Preparation(provider=self.grants.secrets.provider, authorizer=Access()).approval_query()

    def body(self, **kwargs):
        from _create_cases import CreateVertical
        return CreateVertical.body(self, **kwargs)

    def submit(self, *args, **kwargs):
        from _create_cases import CreateVertical
        return CreateVertical.submit(self, *args, **kwargs)

    def test_create_workflow_completes_through_real_bounded_activation_socket(self):
        self.run_create()

    def test_lost_real_socket_reply_is_unknown_and_reconciles_without_reactivation(self):
        self.run_create(lose_reply=True)

    def run_create(self, *, lose_reply=False):
        from _create_cases import CreateActivation, CreateRegistration
        from mop.common.credential_client import GrantActivator
        from mop.common.credentials import Credential
        from mop.server.access import services as auth
        from mop.server.access.models import Grant
        from mop.server.sessions.models import Assignment
        from mop.server.operations.models import Operation
        operation, target, _ = CreateRegistration.launch(self)
        # Методы exercise требуют только Client/path; node результаты штатные.
        self.machine = lambda: CreateActivation.machine(self)
        CreateActivation.through_readiness(self, operation)
        key = auth.enroll_service('socket-activation-worker', self.node.pk, project_ids=(self.project.pk,),
            permissions=('access.activate_grant',), use_case='grant-activator')
        with override_settings(BOOTSTRAP=self.bootstrap):
            app = self.service.application(provider=self.grants.secrets.provider)
            async def body(client, server):
                calls, execute = [], server.application.execute
                def execute_then_lose_reply(*args):
                    calls.append(args[1])
                    value = execute(*args)
                    if lose_reply:
                        raise OSError('fixture reply lost after activation commit')
                    return value
                server.application.execute = execute_then_lose_reply
                self.ops.activations.activator = GrantActivator(client, Credential('token', key))
                result = await server.runtime.database(self.ops.activations.run, operation)
                self.assertEqual(result['state'], 'unknown' if lose_reply else 'succeeded', result['reason'])
                if lose_reply:
                    await server.runtime.database(self.ops.activations.run, operation)
                    await server.runtime.database(self.ops.activations.reconcile, self.actor, operation)
                self.assertEqual(len(calls), 1, 'uncertain activation was replayed')
            async_to_sync(self.exercise)(body, application=app)
        self.assertEqual(Operation.objects.get(pk=operation).state, 'succeeded')
        self.assertEqual(Grant.objects.get(pk=Assignment.objects.get(pk=target).grant_id).state, 'active')
