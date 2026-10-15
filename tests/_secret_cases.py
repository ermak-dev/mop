"""Контракт encrypted ORM core #637 на изолированном PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib
import threading
import uuid
from unittest.mock import Mock, patch

from django.core import serializers
from django.db import IntegrityError, connections, transaction
from django.db.models.deletion import ProtectedError
from django.test import TransactionTestCase

from mop.server import secrets
from mop.server.catalog import models as catalog
from mop.server.controller.runtime import initialize
from mop.server.secrets.crypto import Keyring, XChaCha20Provider, Envelope, SecretCryptoError
from mop.server.secrets.rotation import Effect, Outcome, Observation, FailureReason, Stage

# HYPOTHESIS: AEAD и чистая Rotation не обеспечивают ORM CAS/nonce/receipt fencing.
# SOLUTION: явные store и rotation commands с default deny и короткими транзакциями.
# Red проверяет отсутствие собственного публичного поведения, не чужие imports/setup.
# RESULT: 26 ORM cases проходят на PG16/17, jobs 29440/29441 (pipeline 5457).
# Полные jobs остаются красными на отдельном #632 restore/root check.
# STATUS: FIXED — see #637; это проверка core, не полная приёмка задачи.
class Allow:
    def authorize(self, actor, action, *, project_ids=(), node_ids=(), client_ids=()):
        if actor != 'trusted':
            raise PermissionError('denied')


class Gate:
    def __init__(self, selection, actor='machine'):
        self.selection = selection
        self.actor = actor
        self.calls = 0

    def resolve(self, actor):
        if actor != self.actor:
            raise PermissionError('denied')
        self.calls += 1
        return self.selection


class CurrentVerifier:
    """Явный test-only verifier: текущий полный набор, не operator scope bypass."""
    def __init__(self, recipients):
        self.recipients = tuple(sorted(recipients, key=lambda b: str(b.id)))
        self.calls = []
        self.allowed = True

    def verify_current(self, actor, proof):
        if not connections['default'].in_atomic_block:
            raise AssertionError('current proof must be checked in the lifecycle CAS transaction')
        self.calls.append((actor, proof))
        return (self.allowed and actor == 'trusted' and proof.recipients == self.recipients
                and all(b.secret_id == proof.secret_id for b in proof.recipients)
                and proof.old_version.secret_id == proof.secret_id
                and proof.new_version.secret_id == proof.secret_id)


class SecretCases(TransactionTestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('mop.server.secrets.services'),
                             'missing encrypted ORM store commands')
        self.s = importlib.import_module('mop.server.secrets.services')
        self.m = importlib.import_module('mop.server.secrets.models')
        initialize()
        self.p = catalog.Project.objects.create(name='alpha', origin='https://git.example/alpha.git')
        self.q = catalog.Project.objects.create(name='beta', origin='https://git.example/beta.git')
        self.n = catalog.Node.objects.create(name='n', driver='host', ssh_host='ssh.example',
            control_origin='https://api.example', lan_address='10.0.0.2', bootstrap_address='boot.example')
        self.crypto = XChaCha20Provider(Keyring('k1', {'k1': b'a' * 32}))
        self.api = self.s.SecretStore(provider=self.crypto, authorizer=Allow())
        self.actor = 'trusted'

    def secret(self, name='TOKEN', **kw):
        args = dict(project_id=self.p.pk, name=name, kind='variable', purpose='project', value=b'private-value\x00\xff')
        args.update(kw)
        return self.api.create(self.actor, **args)

    def binding(self, secret):
        return self.api.bind(self.actor, secret.id, expected_revision=secret.revision,
            node_id=self.n.pk, role='worker', digest='a' * 64, incarnation=uuid.uuid4(),
            grant_id=uuid.uuid4(), assignment_id=uuid.uuid4())

    def delivery(self, binding):
        gate = Gate(self.s.DeliverySelection((binding.id,), ((binding.kind, binding.name),)))
        return self.s.SecretStore(provider=self.crypto, authorizer=Allow(), delivery_gate=gate), gate

    def race(self, fn):
        barrier = threading.Barrier(2)
        def worker(i):
            try:
                barrier.wait(timeout=10)
                return fn(i)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(worker, range(2)))

    def test_ciphertext_roundtrip_metadata_and_actual_dump_are_private(self):
        secret = self.secret()
        row = self.m.SecretVersion.objects.get(secret_id=secret.id)
        env = Envelope.from_bytes(bytes(row.envelope))
        self.assertEqual((env.key_id, env.nonce), (row.key_id, bytes(row.nonce)))
        binding = self.binding(secret)
        api, gate = self.delivery(binding)
        issued = api.issue_addressed('machine')
        self.assertEqual(issued[0].value, b'private-value\x00\xff')
        self.assertGreaterEqual(gate.calls, 2)
        for public in (secret, self.api.metadata(self.actor, secret.id), self.api.list(self.actor, project_id=self.p.pk)):
            self.assertNotIn('private-value', repr(public))
            self.assertFalse(hasattr(public, 'envelope'))
        dump = serializers.serialize('json', self.m.SecretVersion.objects.all())
        self.assertNotIn('private-value', dump)
        import base64
        import tempfile
        from mop.server.controller.backup import _tool
        self.assertNotIn(base64.b64encode(b'private-value\x00\xff').decode(), dump)
        with tempfile.TemporaryFile() as raw:
            db = connections['default']
            _tool('pg_dump', db.settings_dict, major=db.pg_version // 10000,
                  output=raw, extra=('--data-only', '--table=secrets_*'))
            raw.seek(0)
            data = raw.read()
        self.assertNotIn(b'private-value', data)
        self.assertNotIn(b'private-value\x00\xff'.hex().encode(), data)
        self.assertNotIn('private-value', repr(row))
        self.assertNotIn('private-value', repr(issued))
        with self.assertRaises(PermissionError):
            self.s.SecretStore(provider=self.crypto).metadata(self.actor, secret.id)
        with self.assertRaises(PermissionError):
            self.api.issue_addressed('machine')

    def test_wrong_root_and_swapped_record_or_scope_cannot_decrypt(self):
        a, b = self.secret(), self.secret('OTHER')
        binding = self.binding(a)
        api, _ = self.delivery(binding)
        wrong = XChaCha20Provider(Keyring('k1', {'k1': b'b' * 32}))
        api.provider = wrong
        with self.assertRaises(SecretCryptoError):
            api.issue_addressed('machine')
        api.provider = self.crypto
        # Намеренная порча БД обходит immutable manager только через base manager.
        row = self.m.SecretVersion.objects.get(secret_id=a.id)
        other = self.m.SecretVersion.objects.get(secret_id=b.id)
        foreign = self.crypto.encrypt(b'foreign-value', self.s.context(other.secret, other))
        self.m.SecretVersion._base_manager.filter(pk=row.pk).update(
            envelope=foreign.to_bytes(), key_id=foreign.key_id, nonce=foreign.nonce)
        with self.assertRaises(SecretCryptoError):
            api.issue_addressed('machine')
        self.m.SecretVersion._base_manager.filter(pk=row.pk).update(
            envelope=row.envelope, key_id=row.key_id, nonce=row.nonce)
        self.m.Secret._base_manager.filter(pk=a.id).update(project_id=self.q.pk)
        with self.assertRaises(SecretCryptoError):
            api.issue_addressed('machine')

    def test_immutable_versions_cas_and_staging_does_not_activate(self):
        a = self.secret()
        staged = self.api.stage(self.actor, a.id, b'new', expected_revision=a.revision)
        current = self.api.metadata(self.actor, a.id)
        self.assertEqual(current.active_version_id, a.active_version_id)
        self.assertEqual(staged.number, 2)
        self.assertEqual(current.revision, 2)
        with self.assertRaises(self.s.Conflict):
            self.api.update(self.actor, a.id, b'stale', expected_revision=1)
        updated = self.api.update(self.actor, a.id, b'configuration', expected_revision=2)
        self.assertNotEqual(updated.active_version_id, a.active_version_id)
        row = self.m.SecretVersion.objects.get(pk=staged.id)
        with self.assertRaises(ValueError):
            row.save()
        with self.assertRaises(ValueError):
            self.m.SecretVersion.objects.filter(pk=row.pk).update(number=99)
        with self.assertRaises(ProtectedError):
            self.m.Secret.objects.get(pk=a.id).delete()
        with self.assertRaises(ProtectedError):
            self.p.delete()

    def test_two_writers_one_parent_revision_and_monotonic_versions(self):
        a = self.secret()
        def write(i):
            try:
                self.api.stage(self.actor, a.id, bytes([i]), expected_revision=1)
                return 'ok'
            except self.s.Conflict:
                return 'conflict'
        self.assertCountEqual(self.race(write), ['ok', 'conflict'])
        self.assertEqual(list(self.m.SecretVersion.objects.filter(secret_id=a.id).order_by('number').values_list('number', flat=True)), [1, 2])

    def test_nonce_unique_in_postgres_and_collision_reencrypts_before_publication(self):
        a = self.secret()
        row = self.m.SecretVersion.objects.get(secret_id=a.id)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.m.SecretVersion._base_manager.create(secret_id=a.id, number=2,
                envelope=row.envelope, key_id=row.key_id, nonce=row.nonce, size=row.size)
        original = self.crypto.encrypt
        calls = []
        class Collision:
            def encrypt(_, value, context):
                calls.append(context)
                if len(calls) == 1:
                    with patch('mop.server.secrets.crypto.utils.random', return_value=bytes(row.nonce)):
                        return original(value, context)
                return original(value, context)
            def decrypt(_, envelope, context):
                return self.crypto.decrypt(envelope, context)
        self.api.provider = Collision()
        new = self.api.stage(self.actor, a.id, b'fresh', expected_revision=1)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(bytes(self.m.SecretVersion.objects.get(pk=new.id).nonce), bytes(row.nonce))
        with self.assertRaises(self.s.Conflict):
            self.api.stage(self.actor, a.id, b'replay', expected_revision=1)

    def test_bounds_names_scopes_and_protected_cross_references(self):
        for name in ('', '../TOKEN', 'A=B', 'a-b', 'X\nY'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.secret(name)
        for name in ('/absolute', '../escape', 'a//b', 'a/./b', 'a\\b'):
            with self.subTest(path=name), self.assertRaises(ValueError):
                self.secret(name, kind='file')
        self.secret('keys/private.pem', kind='file', value=b'')
        self.secret('SSH', project_id=None, purpose='bootstrap_ssh')
        for purpose in ('nomad', 'signer', 'node', 'proxy'):
            self.secret(purpose.upper(), project_id=None, purpose=purpose)
        for value in ('text', bytearray(b'x'), b'x' * (1024 * 1024 + 1)):
            with self.assertRaises(ValueError):
                self.secret(value=value)
        self.secret('MAXIMUM', value=b'x' * (1024 * 1024))
        a = self.secret()
        b = self.secret('B', project_id=self.q.pk)
        with self.assertRaises(ValueError):
            self.m.Secret.objects.filter(pk=a.id).update(active_version_id=b.active_version_id)
        with self.assertRaises(PermissionError):
            self.api.stage('forged', a.id, b'no', expected_revision=1)

    def test_crypto_never_runs_inside_even_caller_transaction(self):
        class AssertOutside:
            def encrypt(_, value, context):
                self.assertFalse(connections['default'].in_atomic_block)
                return self.crypto.encrypt(value, context)
            def decrypt(_, envelope, context):
                self.assertFalse(connections['default'].in_atomic_block)
                return self.crypto.decrypt(envelope, context)
        self.api.provider = AssertOutside()
        with transaction.atomic(), self.assertRaises(self.s.TransactionBoundary):
            self.secret()
        a = self.secret()
        b = self.binding(a)
        api, _ = self.delivery(b)
        api.provider = self.api.provider
        with transaction.atomic(), self.assertRaises(self.s.TransactionBoundary):
            api.issue_addressed('machine')
        self.assertEqual(api.issue_addressed('machine')[0].value, b'private-value\x00\xff')

    def test_reauthorize_after_provider_wait_and_exact_required_allowlist(self):
        a, b = self.secret(), self.secret('OTHER')
        binding = self.binding(a)
        api, gate = self.delivery(binding)
        api.delivery_gate = Gate(self.s.DeliverySelection((binding.id,), (('variable', 'MISSING'),)))
        with self.assertRaises(self.s.Refused):
            api.issue_addressed('machine')
        api.delivery_gate = gate
        class Revoking:
            def decrypt(_, envelope, context):
                value = self.crypto.decrypt(envelope, context)
                self.api.revoke(self.actor, a.id, expected_revision=1)
                return value
        api.provider = Revoking()
        with self.assertRaises(self.s.Conflict):
            api.issue_addressed('machine')

    def test_revocation_after_encrypt_cannot_commit_and_metadata_authorizes_actual_project(self):
        a = self.secret()
        class Restricted:
            allowed = True
            def authorize(_, actor, action, **scope):
                self.assertEqual(scope.get('project_ids'), (self.p.pk,))
                if not _.allowed:
                    raise PermissionError('denied')
        auth = Restricted()
        class Waiting:
            def encrypt(_, value, context):
                env = self.crypto.encrypt(value, context)
                auth.allowed = False
                return env
        api = self.s.SecretStore(provider=Waiting(), authorizer=auth)
        with self.assertRaises(PermissionError):
            api.stage(self.actor, a.id, b'no', expected_revision=1)
        self.assertEqual(self.m.SecretVersion.objects.filter(secret_id=a.id).count(), 1)

    def rotation(self):
        a = self.secret()
        new = self.api.stage(self.actor, a.id, b'new-key', expected_revision=1)
        b = self.binding(self.api.metadata(self.actor, a.id))
        gate = Gate(self.s.DeliverySelection((b.id,), ((b.kind, b.name),)), actor=self.actor)
        service = self.s.RotationService(authorizer=Allow(), delivery_gate=gate,
                                         current_verifier=CurrentVerifier((b,)))
        r = service.stage(self.actor, a.id, new.id, (b.id,), expected_revision=2)
        return service, a, new, b, r

    # HYPOTHESIS: bind под parent lock не проверяет frozen recipient set.
    # Новый binding после stage получает старый ключ без участия в ротации.
    # SOLUTION: no_rotation под существующим parent lock, общим с stage.
    def test_unresolved_credential_rotation_refuses_late_binding_without_old_key_issuance(self):
        a = self.secret('PROXY', project_id=None, purpose='proxy', kind='credential')
        new = self.api.stage(self.actor, a.id, b'new-key', expected_revision=1)
        current = self.api.metadata(self.actor, a.id)
        b = self.binding(current)
        svc = self.s.RotationService(authorizer=Allow())
        r = svc.stage(self.actor, a.id, new.id, (b.id,), expected_revision=current.revision)
        before = self.m.SecretAudit.objects.count()
        with self.assertRaisesRegex(self.s.Conflict, 'unresolved credential rotation'):
            self.binding(current)
        self.assertEqual(self.m.SecretAudit.objects.count(), before)
        self.assertEqual(set(self.m.SecretBinding.objects.filter(secret_id=a.id).values_list('id', flat=True)), {b.id})
        self.assertEqual(set(self.m.RotationRecipient.objects.filter(rotation_id=r.id).values_list('binding_id', flat=True)), {b.id})
        # Даже доверенный gate с несуществующим late binding не выдаёт старый ключ.
        api, _ = self.delivery(replace(b, id=uuid.uuid4()))
        api.provider = Mock(wraps=self.crypto)
        with self.assertRaises(self.s.Refused):
            api.issue_addressed('machine')
        api.provider.decrypt.assert_not_called()
        self.assertEqual(svc.metadata(self.actor, r.id).required, frozenset({str(b.id)}))

    def test_concurrent_rotation_stage_and_bind_share_parent_lock_in_both_orders(self):
        for first in ('stage', 'bind'):
            with self.subTest(first=first):
                a = self.secret('PROXY_' + first, project_id=None, purpose='proxy', kind='credential')
                new = self.api.stage(self.actor, a.id, b'new-key', expected_revision=1)
                current = self.api.metadata(self.actor, a.id)
                b = self.binding(current)
                svc = self.s.RotationService(authorizer=Allow())
                locked, attempted = threading.Event(), threading.Event()
                def command(kind):
                    try:
                        result = (svc.stage(self.actor, a.id, new.id, (b.id,), expected_revision=2)
                                  if kind == 'stage' else self.binding(current))
                        return 'ok', result
                    except (self.s.Conflict, self.s.Refused) as error:
                        return 'refused', error
                def compete(i):
                    if i == 0:
                        with transaction.atomic():
                            self.m.Secret.objects.select_for_update().get(pk=a.id)
                            locked.set()
                            self.assertTrue(attempted.wait(10), 'competing command never requested parent lock')
                            return command(first)
                    self.assertTrue(locked.wait(10), 'first command never locked parent')
                    def observe(execute, sql, params, many, context):
                        if 'FOR UPDATE' in sql.upper():
                            attempted.set()
                        return execute(sql, params, many, context)
                    with connections['default'].execute_wrapper(observe):
                        return command('bind' if first == 'stage' else 'stage')
                results = self.race(compete)
                self.assertEqual([status for status, _ in results], ['ok', 'refused'])
                bound = set(self.m.SecretBinding.objects.filter(secret_id=a.id).values_list('id', flat=True))
                if first == 'stage':
                    self.assertIsInstance(results[1][1], self.s.Conflict)
                    self.assertEqual(bound, {b.id})
                    r = results[0][1]
                else:
                    self.assertIsInstance(results[1][1], self.s.Refused)
                    self.assertEqual(bound, {b.id, results[0][1].id})
                    self.assertFalse(self.m.CredentialRotation.objects.filter(secret_id=a.id).exists())
                    r = svc.stage(self.actor, a.id, new.id, tuple(bound), expected_revision=2)
                self.assertEqual(set(self.m.RotationRecipient.objects.filter(rotation_id=r.id).values_list('binding_id', flat=True)), bound)
                self.assertEqual(svc.metadata(self.actor, r.id).required, frozenset(map(str, bound)))

    # HYPOTHESIS: ручной autocommit=False пропускает provider внутри caller SQL.
    # SOLUTION: общий entry guard проверяет atomic и get_autocommit всех aliases.
    def test_all_provider_entries_refuse_manual_and_atomic_transactions_before_io(self):
        a = self.secret('CONFIG')
        b = self.binding(a)
        api, _ = self.delivery(b)
        svc, _, _, rb, r = self.rotation()
        r = svc.begin(self.actor, r.id, Effect.DELIVER, expected_revision=r.revision)
        rotation_api, _ = self.delivery(rb)
        commands = {
            'create': lambda: self.secret('MANUAL'),
            'update': lambda: self.api.update(self.actor, a.id, b'changed', expected_revision=1),
            'stage': lambda: self.api.stage(self.actor, a.id, b'staged', expected_revision=1),
            'decrypt_addressed': lambda: api.issue_addressed('machine'),
            'decrypt_rotation': lambda: rotation_api.issue_rotation('machine', r.id, claim_id=r.claim_id, generation=r.generation),
        }
        provider = Mock(wraps=self.crypto)
        self.api.provider = api.provider = rotation_api.provider = provider
        connection = connections['default']
        for mode in ('manual', 'atomic'):
            for name, command in commands.items():
                with self.subTest(mode=mode, command=name):
                    provider.reset_mock()
                    if mode == 'manual':
                        connection.set_autocommit(False)
                        try:
                            self.assertFalse(connection.in_atomic_block)
                            with self.assertRaises(self.s.TransactionBoundary):
                                command()
                        finally:
                            connection.rollback()
                            connection.set_autocommit(True)
                    else:
                        with transaction.atomic(), self.assertRaises(self.s.TransactionBoundary):
                            command()
                    provider.encrypt.assert_not_called()
                    provider.decrypt.assert_not_called()
        self.assertEqual(self.api.metadata(self.actor, a.id).revision, 1)
        self.assertEqual(self.m.SecretVersion.objects.filter(secret_id=a.id).count(), 1)
        self.assertFalse(self.m.Secret.objects.filter(name='MANUAL').exists())
        self.assertEqual(api.issue_addressed('machine')[0].value, b'private-value\x00\xff')

    def test_rotation_intent_restart_unknown_partial_and_failed_compensation(self):
        svc, a, new, b, r = self.rotation()
        r = svc.begin(self.actor, r.id, Effect.DELIVER, expected_revision=r.revision)
        self.assertIsNotNone(r.claim_id)
        restarted = self.s.RotationService(authorizer=Allow(), current_verifier=svc.current_verifier)
        r = restarted.record(self.actor, r.id, Outcome.UNKNOWN, claim_id=r.claim_id,
                             expected_revision=r.revision)
        self.assertEqual(r.state, Stage.RECONCILING)
        with self.assertRaises(ValueError):
            restarted.begin(self.actor, r.id, Effect.DELIVER, expected_revision=r.revision)
        r = restarted.reconcile(self.actor, r.id, Observation.PARTIAL, claim_id=r.claim_id,
                                quiescent=True, expected_revision=r.revision)
        self.assertEqual(r.state, Stage.RECOVERY_REQUIRED)
        r = restarted.begin(self.actor, r.id, Effect.RESTORE_OLD, expected_revision=r.revision)
        r = restarted.record(self.actor, r.id, Outcome.REJECTED, claim_id=r.claim_id,
                             reason=FailureReason.UNAVAILABLE, expected_revision=r.revision)
        self.assertEqual((r.state, r.original_reason, r.compensation_reason),
                         (Stage.PARTIAL, FailureReason.PARTIAL, FailureReason.UNAVAILABLE))
        self.assertEqual(self.api.metadata(self.actor, a.id).active_version_id, a.active_version_id)

    def test_rotation_activation_only_exact_receipts_and_stale_claim_refused(self):
        svc, a, new, b, r = self.rotation()
        r = svc.begin(self.actor, r.id, Effect.DELIVER, expected_revision=r.revision)
        with self.assertRaises(ValueError):
            svc.record(self.actor, r.id, Outcome.APPLIED, claim_id=r.claim_id, expected_revision=r.revision)
        with self.assertRaises(self.s.Conflict):
            svc.receipt(self.actor, r.id, b.id, version_id=new.id, claim_id=uuid.uuid4(), generation=r.generation, outcome=Outcome.APPLIED)
        receipt = svc.receipt(self.actor, r.id, b.id, version_id=new.id, claim_id=r.claim_id,
                              generation=r.generation, outcome=Outcome.APPLIED)
        duplicate = svc.receipt(self.actor, r.id, b.id, version_id=new.id, claim_id=r.claim_id,
                                generation=r.generation, outcome=Outcome.APPLIED)
        self.assertEqual(receipt, duplicate)
        r = svc.record(self.actor, r.id, Outcome.APPLIED, claim_id=r.claim_id, expected_revision=r.revision)
        self.assertEqual(self.api.metadata(self.actor, a.id).active_version_id, a.active_version_id)
        r = svc.activate(self.actor, r.id, expected_revision=r.revision)
        self.assertEqual(self.api.metadata(self.actor, a.id).active_version_id, new.id)
        with self.assertRaises(self.s.Conflict):
            svc.receipt(self.actor, r.id, b.id, version_id=new.id, claim_id=r.claim_id,
                        generation=r.generation, outcome=Outcome.APPLIED)

    def test_rotation_unique_and_concurrent_intent_cas(self):
        svc, a, new, b, r = self.rotation()
        with self.assertRaises(self.s.Conflict):
            svc.stage(self.actor, a.id, new.id, (b.id,), expected_revision=2)
        def begin(_):
            try:
                svc.begin(self.actor, r.id, Effect.DELIVER, expected_revision=r.revision)
                return 'ok'
            except self.s.Conflict:
                return 'conflict'
        self.assertCountEqual(self.race(begin), ['ok', 'conflict'])
        self.assertEqual(self.m.CredentialRotation.objects.count(), 1)
        with self.assertRaises(self.s.Conflict):
            self.api.update(self.actor, a.id, b'newer', expected_revision=2)

    def test_rotation_recovery_cannot_overwrite_newer_secret_decision(self):
        svc, a, new, b, r = self.rotation()
        r = svc.begin(self.actor, r.id, Effect.DELIVER, expected_revision=r.revision)
        r = svc.record(self.actor, r.id, Outcome.UNKNOWN, claim_id=r.claim_id, expected_revision=r.revision)
        r = svc.reconcile(self.actor, r.id, Observation.PARTIAL, claim_id=r.claim_id,
                          quiescent=True, expected_revision=r.revision)
        self.api.revoke(self.actor, a.id, expected_revision=2)
        with self.assertRaises(self.s.Conflict):
            svc.begin(self.actor, r.id, Effect.RESTORE_OLD, expected_revision=r.revision)

    def test_rotation_cross_secret_recipient_and_unknown_receipt_refused(self):
        svc, a, new, b, r = self.rotation()
        other = self.binding(self.secret('OTHER'))
        r = svc.begin(self.actor, r.id, Effect.DELIVER, expected_revision=r.revision)
        for binding_id, version_id in ((other.id, new.id), (b.id, a.active_version_id)):
            with self.assertRaises(self.s.Refused):
                svc.receipt(self.actor, r.id, binding_id, version_id=version_id,
                    claim_id=r.claim_id, generation=r.generation, outcome=Outcome.APPLIED)
        with self.assertRaises(ValueError):
            self.m.RotationRecipient.objects.create(rotation_id=r.id, binding_id=other.id)
        with self.assertRaises(ProtectedError):
            self.m.SecretBinding.objects.get(pk=b.id).delete()

    def test_rotation_timeout_preserves_intent_until_explicit_quiescent_readback(self):
        svc, a, new, b, r = self.rotation()
        r = svc.begin(self.actor, r.id, Effect.DELIVER, expected_revision=r.revision)
        claim = r.claim_id
        r = svc.record(self.actor, r.id, Outcome.UNKNOWN, claim_id=claim, expected_revision=r.revision)
        self.assertEqual(r.pending, Effect.DELIVER)
        with self.assertRaises(ValueError):
            svc.reconcile(self.actor, r.id, Observation.ABSENT, claim_id=claim, expected_revision=r.revision)
        r = svc.reconcile(self.actor, r.id, Observation.ABSENT, claim_id=claim,
                          quiescent=True, expected_revision=r.revision)
        r = svc.begin(self.actor, r.id, Effect.DELIVER, expected_revision=r.revision)
        self.assertNotEqual(r.claim_id, claim)
        self.assertEqual(r.generation, 2)

    def test_rotation_issues_staged_bytes_only_to_current_claim_and_address(self):
        svc, a, new, b, r = self.rotation()
        api, gate = self.delivery(b)
        self.assertTrue(callable(getattr(api, 'issue_rotation', None)),
                        'staged credentials require an explicit claim-bound issuer')
        with self.assertRaises(self.s.Conflict):
            api.issue_rotation('machine', r.id, claim_id=uuid.uuid4(), generation=1)
        r = svc.begin(self.actor, r.id, Effect.DELIVER, expected_revision=r.revision)
        issued = api.issue_rotation('machine', r.id, claim_id=r.claim_id, generation=r.generation)
        self.assertEqual((issued[0].version_id, issued[0].value), (new.id, b'new-key'))
        self.assertEqual(api.issue_addressed('machine')[0].value, b'private-value\x00\xff')
        with self.assertRaises(self.s.Conflict):
            api.issue_rotation('machine', r.id, claim_id=uuid.uuid4(), generation=r.generation)

    def test_credential_update_cannot_bypass_rotation(self):
        proxy = self.secret('PROXY', project_id=None, purpose='proxy', kind='credential')
        with self.assertRaises(self.s.Refused):
            self.api.update(self.actor, proxy.id, b'bypass', expected_revision=1)

    def test_database_rejects_non_enum_rotation_reasons(self):
        svc, a, new, b, r = self.rotation()
        for fields in ({'pending': 'arbitrary'}, {'original_reason': 'private-value'},
                       {'original_failure': 'deliver', 'original_reason': None}):
            with self.subTest(fields=fields), self.assertRaises(IntegrityError), transaction.atomic():
                self.m.CredentialRotation._base_manager.filter(pk=r.id).update(**fields)

    def test_concurrent_nonce_collision_is_reencrypted_with_independent_connections(self):
        barrier = threading.Barrier(2)
        seen = threading.local()
        def nonce(size):
            if not getattr(seen, 'once', False):
                seen.once = True
                barrier.wait(timeout=10)
                return b'n' * size
            from os import urandom
            return urandom(size)
        with patch('mop.server.secrets.crypto.utils.random', side_effect=nonce):
            results = self.race(lambda i: self.secret('PARALLEL_' + str(i)))
        self.assertEqual(len(results), 2)
        self.assertEqual(self.m.SecretVersion.objects.values('key_id', 'nonce').distinct().count(), 2)

    def test_receipt_default_gate_does_not_trust_machine_or_permission_alone(self):
        svc, a, new, b, r = self.rotation()
        r = svc.begin(self.actor, r.id, Effect.DELIVER, expected_revision=r.revision)
        unbound = self.s.RotationService(authorizer=Allow())
        with self.assertRaises(PermissionError):
            unbound.receipt(self.actor, r.id, b.id, version_id=new.id, claim_id=r.claim_id,
                            generation=r.generation, outcome=Outcome.APPLIED)

    def test_storage_and_provider_failures_do_not_retain_raw_secret_exception(self):
        from django.db import DatabaseError
        self.assertTrue(hasattr(self.s, 'StorageUnavailable'), 'storage failures require a typed safe reason')
        a = self.secret()
        with patch.object(self.m.Secret.objects, 'get', side_effect=DatabaseError('private-value')):
            try:
                self.api.metadata(self.actor, a.id)
            except self.s.StorageUnavailable as error:
                self.assertNotIn('private-value', str(error))
                self.assertIsNone(error.__context__)
            else:
                self.fail('storage failure was swallowed')
        class Broken:
            def encrypt(_, value, context):
                raise RuntimeError('private-value')
        api = self.s.SecretStore(provider=Broken(), authorizer=Allow())
        try:
            api.stage(self.actor, a.id, b'private-value', expected_revision=1)
        except SecretCryptoError as error:
            self.assertNotIn('private-value', str(error))
            self.assertIsNone(error.__context__)
        else:
            self.fail('provider failure was swallowed')

    def test_file_metadata_exposes_size_without_content(self):
        a = self.secret('keys/private.pem', kind='file')
        self.assertEqual(getattr(self.api.metadata(self.actor, a.id), 'size', None), len(b'private-value\x00\xff'))
        self.assertNotIn('private-value', repr(self.api.list(self.actor, project_id=self.p.pk)))

    def test_retirement_and_compensation_have_persisted_crash_checkpoints(self):
        svc, a, new, b, r = self.rotation()
        r = svc.begin(self.actor, r.id, Effect.DELIVER, expected_revision=r.revision)
        svc.receipt(self.actor, r.id, b.id, version_id=new.id, claim_id=r.claim_id,
                    generation=r.generation, outcome=Outcome.APPLIED)
        r = svc.record(self.actor, r.id, Outcome.APPLIED, claim_id=r.claim_id, expected_revision=r.revision)
        r = svc.activate(self.actor, r.id, expected_revision=r.revision)
        r = svc.begin(self.actor, r.id, Effect.RETIRE_OLD, expected_revision=r.revision)
        restarted = self.s.RotationService(authorizer=Allow(), delivery_gate=svc.delivery_gate,
                                           current_verifier=svc.current_verifier)
        self.assertEqual(restarted.metadata(self.actor, r.id).pending, Effect.RETIRE_OLD)
        r = restarted.record(self.actor, r.id, Outcome.REJECTED, claim_id=r.claim_id,
                             expected_revision=r.revision, reason=FailureReason.REFUSED)
        r = restarted.begin(self.actor, r.id, Effect.RESTORE_OLD, expected_revision=r.revision)
        r = restarted.record(self.actor, r.id, Outcome.UNKNOWN, claim_id=r.claim_id, expected_revision=r.revision)
        with self.assertRaises(self.s.Refused):
            restarted.reconcile(self.actor, r.id, Observation.APPLIED, claim_id=r.claim_id, expected_revision=r.revision)
        restarted.receipt(self.actor, r.id, b.id, version_id=a.active_version_id, claim_id=r.claim_id,
                          generation=r.generation, outcome=Outcome.APPLIED)
        r = restarted.reconcile(self.actor, r.id, Observation.APPLIED, claim_id=r.claim_id, expected_revision=r.revision)
        self.assertEqual(r.state, Stage.OLD_RESTORED)
        self.assertEqual(self.api.metadata(self.actor, a.id).active_version_id, a.active_version_id)
        r = restarted.begin(self.actor, r.id, Effect.REVOKE_NEW, expected_revision=r.revision)
        self.assertEqual(self.s.RotationService(authorizer=Allow()).metadata(self.actor, r.id).pending, Effect.REVOKE_NEW)
        r = restarted.record(self.actor, r.id, Outcome.UNKNOWN, claim_id=r.claim_id, expected_revision=r.revision)
        r = restarted.reconcile(self.actor, r.id, Observation.APPLIED, claim_id=r.claim_id, expected_revision=r.revision)
        self.assertEqual(r.state, Stage.COMPENSATED)
        self.assertTrue(self.m.CredentialRotation.objects.get(pk=r.id).resolved)
        self.assertEqual(r.original_reason, FailureReason.REFUSED)

    def test_unknown_transport_result_is_not_an_immutable_receipt_that_blocks_readback(self):
        svc, a, new, b, r = self.rotation()
        r = svc.begin(self.actor, r.id, Effect.DELIVER, expected_revision=r.revision)
        with self.assertRaises(self.s.Refused):
            svc.receipt(self.actor, r.id, b.id, version_id=new.id, claim_id=r.claim_id,
                        generation=r.generation, outcome=Outcome.UNKNOWN)
        r = svc.record(self.actor, r.id, Outcome.UNKNOWN, claim_id=r.claim_id, expected_revision=r.revision)
        svc.receipt(self.actor, r.id, b.id, version_id=new.id, claim_id=r.claim_id,
                    generation=r.generation, outcome=Outcome.APPLIED)
        r = svc.reconcile(self.actor, r.id, Observation.APPLIED, claim_id=r.claim_id, expected_revision=r.revision)
        self.assertEqual(r.receipts, r.required)

    def test_absent_readback_cannot_discard_confirmed_delivery_evidence(self):
        svc, a, new, b, r = self.rotation()
        r = svc.begin(self.actor, r.id, Effect.DELIVER, expected_revision=r.revision)
        svc.receipt(self.actor, r.id, b.id, version_id=new.id, claim_id=r.claim_id,
                    generation=r.generation, outcome=Outcome.APPLIED)
        r = svc.record(self.actor, r.id, Outcome.UNKNOWN, claim_id=r.claim_id, expected_revision=r.revision)
        with self.assertRaises(self.s.Refused):
            svc.reconcile(self.actor, r.id, Observation.ABSENT, claim_id=r.claim_id,
                          expected_revision=r.revision, quiescent=True)
        r = svc.reconcile(self.actor, r.id, Observation.PARTIAL, claim_id=r.claim_id,
                          expected_revision=r.revision, quiescent=True)
        self.assertEqual(r.receipts, r.required)
        self.assertEqual(r.state, Stage.RECOVERY_REQUIRED)

    # HYPOTHESIS: исторические receipts и operator scope обходят текущее назначение.
    # Membership не отделён от immutable evidence, поэтому stage требует умершие incarnation.
    # SOLUTION: explicit retirement + parent lock/no_rotation; отдельный ORM-only
    # verifier получает полный frozen контекст перед begin/activation/dispatch после wait.
    # UNKNOWN остаётся durable наблюдением, а не разрешением нового effect.
    # RESULT: 39 secret cases green на изолированных PG16/17; общий controller run
    # всё ещё red только на отдельном foundation root-restore check (72 tests, 1 failure).
    # STATUS: FIXED — see #637; реальная композиция verifier из #639 не проверена.
    def delivered(self):
        svc, a, new, b, r = self.rotation()
        r = svc.begin(self.actor, r.id, Effect.DELIVER, expected_revision=r.revision)
        svc.receipt(self.actor, r.id, b.id, version_id=new.id, claim_id=r.claim_id,
                    generation=r.generation, outcome=Outcome.APPLIED)
        r = svc.record(self.actor, r.id, Outcome.APPLIED, claim_id=r.claim_id, expected_revision=r.revision)
        return svc, a, new, b, r

    def test_old_receipts_cannot_activate_without_bound_current_assignment_verifier(self):
        svc, a, new, b, r = self.delivered()
        unbound = self.s.RotationService(authorizer=Allow(), delivery_gate=svc.delivery_gate)
        with self.assertRaises(PermissionError):
            unbound.activate(self.actor, r.id, expected_revision=r.revision)
        self.assertEqual(self.api.metadata(self.actor, a.id).active_version_id, a.active_version_id)

    def test_old_receipts_cannot_override_current_recipient_denial(self):
        svc, a, new, b, r = self.delivered()
        class Revoked:
            def verify_current(_, actor, proof):
                return False
        svc.current_verifier = Revoked()
        svc.delivery_gate.selection = self.s.DeliverySelection((), ())
        with self.assertRaises(self.s.Refused):
            svc.activate(self.actor, r.id, expected_revision=r.revision)
        self.assertIsNone(svc.metadata(self.actor, r.id).pending)

    def test_binding_retirement_keeps_evidence_but_stage_freezes_current_only(self):
        a = self.secret()
        old = self.binding(a)
        retire = getattr(self.api, 'retire_binding', None)
        self.assertTrue(callable(retire), 'binding membership needs explicit retirement, not deletion')
        retire(self.actor, old.id, expected_revision=a.revision)
        self.assertIsNotNone(self.m.SecretBinding.objects.get(pk=old.id).retired_at)
        current = self.binding(a)
        new = self.api.stage(self.actor, a.id, b'new', expected_revision=a.revision)
        svc = self.s.RotationService(authorizer=Allow())
        r = svc.stage(self.actor, a.id, new.id, (current.id,), expected_revision=2)
        self.assertEqual(r.required, frozenset((str(current.id),)))
        self.assertTrue(self.m.SecretBinding.objects.filter(pk=old.id).exists())
        api, _ = self.delivery(old)
        with self.assertRaises(self.s.Refused):
            api.issue_addressed('machine')

    def test_repeated_bind_returns_same_current_membership_without_extra_evidence(self):
        a = self.secret()
        b = self.binding(a)
        repeated = self.api.bind(self.actor, a.id, expected_revision=a.revision,
            node_id=b.node_id, role=b.role, digest=b.digest, incarnation=b.incarnation,
            grant_id=b.grant_id, assignment_id=b.assignment_id)
        self.assertEqual(repeated.id, b.id)
        self.assertEqual(self.m.SecretBinding.objects.filter(secret_id=a.id).count(), 1)

    def test_verifier_sees_full_frozen_context_and_changed_current_facts_cannot_be_overridden(self):
        svc, a, new, b, r = self.delivered()
        verifier = svc.current_verifier
        mutations = ({'incarnation': uuid.uuid4()}, {'grant_id': uuid.uuid4()},
                     {'digest': 'b' * 64}, {'assignment_id': uuid.uuid4()},
                     {'role': 'other'}, {'node_id': uuid.uuid4()})
        for changes in mutations:
            with self.subTest(changes=changes):
                verifier.recipients = (replace(b, **changes),)
                with self.assertRaises(self.s.Refused):
                    svc.activate(self.actor, r.id, expected_revision=r.revision)
        verifier.recipients = (b,)
        verifier.allowed = False  # Active grant/ancestor/owner revoked, same IDs.
        with self.assertRaises(self.s.Refused):
            svc.activate(self.actor, r.id, expected_revision=r.revision)
        verifier.allowed = True
        svc.delivery_gate = Gate(self.s.DeliverySelection((), ()), actor='not-the-operator')
        # Operator не притворяется recipient: отдельный полный verifier, не resolve(operator).
        r = svc.activate(self.actor, r.id, expected_revision=r.revision)
        actor, proof = verifier.calls[-1]
        self.assertEqual(actor, self.actor)
        self.assertEqual(proof.recipients, (b,))
        self.assertEqual((proof.rotation_id, proof.secret_id, proof.project_id), (r.id, a.id, self.p.pk))
        self.assertEqual((proof.old_version.id, proof.new_version.id), (a.active_version_id, new.id))
        self.assertEqual((proof.old_version.number, proof.new_version.number), (1, 2))
        self.assertEqual((proof.name, proof.kind, proof.purpose), ('TOKEN', 'variable', 'project'))
        self.assertEqual(proof.secret_revision, 2)
        self.assertEqual(proof.installation_id, 1)
        self.assertEqual(proof.installation_identity, self.m.Secret.objects.get(pk=a.id).installation.identity)
        verifier.allowed = False
        with self.assertRaises(self.s.Refused):
            svc.begin(self.actor, r.id, Effect.RETIRE_OLD, expected_revision=r.revision)
        verifier.allowed = True
        r = svc.begin(self.actor, r.id, Effect.RETIRE_OLD, expected_revision=r.revision)
        self.assertEqual(r.pending, Effect.RETIRE_OLD)

    def test_after_effect_wait_current_proof_is_required_but_unknown_evidence_can_be_saved(self):
        svc, a, new, b, r = self.rotation()
        r = svc.begin(self.actor, r.id, Effect.DELIVER, expected_revision=r.revision)
        check = getattr(svc, 'verify_effect', None)
        self.assertTrue(callable(check), 'executor needs current full proof after waits, not claim alone')
        checked = check(self.actor, r.id, claim_id=r.claim_id, generation=r.generation,
                        expected_revision=r.revision)
        self.assertEqual(checked, r)
        for claim, generation in ((uuid.uuid4(), r.generation), (r.claim_id, r.generation + 1)):
            with self.assertRaises(self.s.Conflict):
                check(self.actor, r.id, claim_id=claim, generation=generation, expected_revision=r.revision)
        unbound = self.s.RotationService(authorizer=Allow())
        with self.assertRaises(PermissionError):
            unbound.verify_effect(self.actor, r.id, claim_id=r.claim_id, generation=r.generation,
                                  expected_revision=r.revision)
        svc.current_verifier.allowed = False
        with self.assertRaises(self.s.Refused):
            check(self.actor, r.id, claim_id=r.claim_id, generation=r.generation, expected_revision=r.revision)
        # Отзыв назначения не должен стирать или запрещать durable UNKNOWN.
        r = svc.record(self.actor, r.id, Outcome.UNKNOWN, claim_id=r.claim_id, expected_revision=r.revision)
        with self.assertRaises(self.s.Conflict):
            check(self.actor, r.id, claim_id=r.claim_id, generation=r.generation, expected_revision=r.revision)
        r = svc.reconcile(self.actor, r.id, Observation.PARTIAL, claim_id=r.claim_id,
                          quiescent=True, expected_revision=r.revision)
        with self.assertRaises(self.s.Refused):
            svc.begin(self.actor, r.id, Effect.RESTORE_OLD, expected_revision=r.revision)
        svc.current_verifier.allowed = True
        r = svc.begin(self.actor, r.id, Effect.RESTORE_OLD, expected_revision=r.revision)
        svc.receipt(self.actor, r.id, b.id, version_id=a.active_version_id, claim_id=r.claim_id,
                    generation=r.generation, outcome=Outcome.APPLIED)
        svc.current_verifier.allowed = False
        with self.assertRaises(self.s.Refused):
            svc.record(self.actor, r.id, Outcome.APPLIED, claim_id=r.claim_id, expected_revision=r.revision)
        r = svc.record(self.actor, r.id, Outcome.UNKNOWN, claim_id=r.claim_id, expected_revision=r.revision)
        with self.assertRaises(self.s.Refused):
            svc.reconcile(self.actor, r.id, Observation.APPLIED, claim_id=r.claim_id, expected_revision=r.revision)
        svc.current_verifier.allowed = True
        r = svc.reconcile(self.actor, r.id, Observation.APPLIED, claim_id=r.claim_id, expected_revision=r.revision)
        svc.current_verifier.allowed = False
        with self.assertRaises(self.s.Refused):
            svc.begin(self.actor, r.id, Effect.REVOKE_NEW, expected_revision=r.revision)

    def test_payload_cannot_supply_verifier_or_scope_override(self):
        svc, a, new, b, r = self.delivered()
        with self.assertRaises(TypeError):
            svc.activate(self.actor, r.id, expected_revision=r.revision,
                         current_verifier=CurrentVerifier((b,)))
        class ScopeOnly:
            def authorize(_, actor, action, **scope):
                pass
        svc.authorizer = ScopeOnly()
        with self.assertRaises(self.s.Refused):
            svc.activate({'current_verifier': CurrentVerifier((b,)), 'scope_override': True},
                         r.id, expected_revision=r.revision)

    def test_current_binding_slot_is_explicit_and_retired_identity_stays_immutable(self):
        a = self.secret()
        b = self.binding(a)
        with self.assertRaises(self.s.Conflict):
            self.binding(a)  # Тот же node/role, но другая incarnation не supersede автоматически.
        retired = self.api.retire_binding(self.actor, b.id, expected_revision=a.revision)
        self.assertEqual(retired, self.api.retire_binding(self.actor, b.id, expected_revision=a.revision))
        row = self.m.SecretBinding.objects.get(pk=b.id)
        row.retired_at = None
        with self.assertRaises(ValueError):
            row.save()
        row.refresh_from_db()
        row.digest = 'b' * 64
        with self.assertRaises(ValueError):
            row.save()
        current = self.binding(a)
        self.assertNotEqual(current.id, b.id)
        self.assertEqual(self.m.SecretAudit.objects.filter(action='retire_binding', secret_id=a.id).count(), 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.m.SecretBinding._base_manager.create(secret_id=a.id, node_id=b.node_id,
                role=b.role, digest=b.digest, incarnation=uuid.uuid4(), grant_id=uuid.uuid4(),
                assignment_id=uuid.uuid4())

    def test_current_membership_race_serializes_with_rotation_freeze(self):
        svc, a, new, b, r = self.rotation()
        with self.assertRaises(self.s.Conflict):
            self.api.retire_binding(self.actor, b.id, expected_revision=2)
        with self.assertRaises(self.s.Conflict):
            self.api.bind(self.actor, a.id, expected_revision=2, node_id=b.node_id,
                role='another-role', digest=b.digest, incarnation=uuid.uuid4(),
                grant_id=uuid.uuid4(), assignment_id=uuid.uuid4())
        # Current exact lost-response retry — чтение того же решения, без изменения set.
        self.assertEqual(self.api.bind(self.actor, a.id, expected_revision=2, node_id=b.node_id,
            role=b.role, digest=b.digest, incarnation=b.incarnation,
            grant_id=b.grant_id, assignment_id=b.assignment_id).id, b.id)
        with self.assertRaises(ValueError):
            self.m.SecretBinding.objects.create(secret_id=a.id, node_id=b.node_id,
                role='another-role', digest=b.digest, incarnation=uuid.uuid4(),
                grant_id=uuid.uuid4(), assignment_id=uuid.uuid4())

    def test_binding_stage_and_retirement_race_with_independent_connections(self):
        a = self.secret()
        b = self.binding(a)
        new = self.api.stage(self.actor, a.id, b'new', expected_revision=1)
        svc = self.s.RotationService(authorizer=Allow())
        def act(i):
            try:
                if i:
                    svc.stage(self.actor, a.id, new.id, (b.id,), expected_revision=2)
                else:
                    self.api.retire_binding(self.actor, b.id, expected_revision=2)
                return 'ok'
            except (self.s.Conflict, self.s.Refused):
                return 'conflict'
        self.assertCountEqual(self.race(act), ['ok', 'conflict'])
        current = self.m.SecretBinding.objects.get(pk=b.id)
        self.assertEqual(current.retired_at is None,
                         self.m.CredentialRotation.objects.filter(secret_id=a.id).exists())

    def test_retired_binding_preserves_old_rotation_receipts_and_does_not_block_next_set(self):
        svc, a, new, old, r = self.delivered()
        r = svc.activate(self.actor, r.id, expected_revision=r.revision)
        r = svc.begin(self.actor, r.id, Effect.RETIRE_OLD, expected_revision=r.revision)
        r = svc.record(self.actor, r.id, Outcome.APPLIED, claim_id=r.claim_id, expected_revision=r.revision)
        self.api.retire_binding(self.actor, old.id, expected_revision=3)
        current = self.binding(self.api.metadata(self.actor, a.id))
        next_version = self.api.stage(self.actor, a.id, b'next', expected_revision=3)
        next_rotation = svc.stage(self.actor, a.id, next_version.id, (current.id,), expected_revision=4)
        self.assertEqual(next_rotation.required, frozenset((str(current.id),)))
        self.assertEqual(svc.metadata(self.actor, r.id).required, frozenset((str(old.id),)))
        self.assertTrue(self.m.DeliveryReceipt.objects.filter(recipient__binding_id=old.id,
                                                             recipient__rotation_id=r.id).exists())
        with self.assertRaises(ProtectedError):
            self.m.SecretBinding.objects.get(pk=old.id).delete()
        # Даже ошибочный gate со старым id не выдаёт новый credential.
        r2 = self.s.RotationService(authorizer=Allow(), current_verifier=CurrentVerifier((current,)))
        pending = r2.begin(self.actor, next_rotation.id, Effect.DELIVER, expected_revision=next_rotation.revision)
        api, _ = self.delivery(old)
        with self.assertRaises(self.s.Refused):
            api.issue_rotation('machine', pending.id, claim_id=pending.claim_id, generation=pending.generation)

    def test_full_current_set_includes_offline_recipients_without_operator_delivery_selection(self):
        a = self.secret()
        first = self.binding(a)
        second = self.api.bind(self.actor, a.id, expected_revision=1, node_id=self.n.pk,
            role='offline-role', digest='b' * 64, incarnation=uuid.uuid4(),
            grant_id=uuid.uuid4(), assignment_id=uuid.uuid4())
        new = self.api.stage(self.actor, a.id, b'new', expected_revision=1)
        gate = Gate(self.s.DeliverySelection((first.id,), ((first.kind, first.name),)), actor=self.actor)
        verifier = CurrentVerifier((first, second))
        svc = self.s.RotationService(authorizer=Allow(), delivery_gate=gate, current_verifier=verifier)
        with self.assertRaises(self.s.Refused):
            svc.stage(self.actor, a.id, new.id, (first.id,), expected_revision=2)
        r = svc.stage(self.actor, a.id, new.id, (first.id, second.id), expected_revision=2)
        r = svc.begin(self.actor, r.id, Effect.DELIVER, expected_revision=r.revision)
        for binding in (first, second):
            gate.selection = self.s.DeliverySelection((binding.id,), ((binding.kind, binding.name),))
            svc.receipt(self.actor, r.id, binding.id, version_id=new.id, claim_id=r.claim_id,
                        generation=r.generation, outcome=Outcome.APPLIED)
        r = svc.record(self.actor, r.id, Outcome.APPLIED, claim_id=r.claim_id, expected_revision=r.revision)
        svc.delivery_gate = Gate(self.s.DeliverySelection((), ()), actor='machine-not-operator')
        r = svc.activate(self.actor, r.id, expected_revision=r.revision)
        self.assertEqual(r.state, Stage.ACTIVE)
        self.assertEqual(set(b.id for b in verifier.calls[-1][1].recipients), {first.id, second.id})

    def test_binding_retired_during_provider_wait_cannot_return_credential(self):
        a = self.secret()
        b = self.binding(a)
        api, _ = self.delivery(b)
        class Waiting:
            def decrypt(_, envelope, context):
                value = self.crypto.decrypt(envelope, context)
                self.api.retire_binding(self.actor, b.id, expected_revision=a.revision)
                return value
        api.provider = Waiting()
        with self.assertRaises(self.s.Refused):
            api.issue_addressed('machine')

    def test_action_permissions_are_registered_without_default_duplicate(self):
        from django.contrib.auth.models import Permission
        codes = set(Permission.objects.filter(content_type__app_label='secrets').values_list('codename', flat=True))
        self.assertEqual(codes, {'view_secret_metadata', 'manage_project_secret', 'deliver_secret', 'rotate_proxy_key'})
