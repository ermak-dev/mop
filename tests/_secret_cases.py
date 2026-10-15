"""Контракт encrypted ORM core #637 на изолированном PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import importlib
import threading
import uuid
from unittest.mock import patch

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
class Allow:
    def authorize(self, actor, action, *, project_ids=(), node_ids=(), client_ids=()):
        if actor != 'trusted':
            raise PermissionError('denied')


class Gate:
    def __init__(self, selection):
        self.selection = selection
        self.calls = 0

    def resolve(self, actor):
        if actor != 'machine':
            raise PermissionError('denied')
        self.calls += 1
        return self.selection


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
                    return Envelope.from_bytes(bytes(row.envelope))
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
        service = self.s.RotationService(authorizer=Allow())
        r = service.stage(self.actor, a.id, new.id, (b.id,), expected_revision=2)
        return service, a, new, b, r

    def test_rotation_intent_restart_unknown_partial_and_failed_compensation(self):
        svc, a, new, b, r = self.rotation()
        r = svc.begin(self.actor, r.id, Effect.DELIVER, expected_revision=r.revision)
        self.assertIsNotNone(r.claim_id)
        restarted = self.s.RotationService(authorizer=Allow())
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

    def test_action_permissions_are_registered_without_default_duplicate(self):
        from django.contrib.auth.models import Permission
        codes = set(Permission.objects.filter(content_type__app_label='secrets').values_list('codename', flat=True))
        self.assertEqual(codes, {'view_secret_metadata', 'manage_project_secret', 'deliver_secret', 'rotate_proxy_key'})
