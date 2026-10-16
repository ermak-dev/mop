"""Контракты immutable ролей и делегирования #635 на настоящем PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import importlib
import json
from threading import Barrier
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import serializers
from django.db import connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token

from mop.common import soul
from mop.server.access import models as access, services as auth
from mop.server.catalog.models import Project
from mop.server.controller.runtime import initialize
from mop.server.secrets.crypto import Keyring, XChaCha20Provider

# HYPOTHESIS: файловый registry не защищает ORM request/CAS и live Django authority.
# SOLUTION: immutable bundle + creation intent + отдельные lifecycle/approval/binding ports.
# RED: pipeline 5467, jobs 29470/29471 — отсутствуют собственные RoleBundle/retention.
# Дополнительный red: отзыв target token, ciphertext activation, shared prompt read,
# ошибка commit с key в сообщении и отзыв source во время provider wait.
# RESULT: все 33 собственных cases проходят на изолированных PG16/17; из 131 cases
# остаётся только прежний Foundation restore/root failure. Пул не использовался.
# STATUS: FIXED — see #635; late approval/runtime/executor bindings ещё не приёмка.
# Combined accepted dependencies: migration seeds сохранены serialized_rollback;
# прежний restore/root setup failure больше не основание пропускать Grant checks.


def manifest(role='worker', *, spawn=(), dispatch=False, receives=True):
    return {'soul': role, 'version': 1, 'runtime': {'driver': 'any', 'receives_dispatch': receives},
            'capabilities': {'node_verbs': ['send', 'state'], 'cluster_verbs': ['add', 'roster'],
                             'dispatch': dispatch, 'spawn': list(spawn)}, 'prompt': 'soul.md'}


class ApprovalFixture:
    """Доверенный локальный query; меняется approval, но не ранее принятые bytes."""
    def __init__(self, proof):
        self.proof, self.calls, self.enabled = proof, 0, True

    def pin(self, binding):
        self.calls += 1
        self.validate(self.proof, binding)
        return self.proof

    def validate(self, proof, binding):
        if not self.enabled or (proof.owner_id, proof.project_id) != (binding.owner_id, binding.project_id):
            raise PermissionError('approved policy is unavailable')


class BindingFixture:
    def __init__(self):
        self.bindings, self.receipts = {}, {}

    def current(self, project_id, target_id):
        return self.bindings.get((project_id, target_id))

    def activation(self, grant_id):
        return self.receipts.get(grant_id)


class RetainedSecretContract(TransactionTestCase):
    # RestoreGuard и прочие migration seeds сохраняются после flush.
    serialized_rollback = True

    def test_prepared_retention_is_public_atomic_target_bound_and_default_deny(self):
        from mop.server.secrets import services as secrets
        self.assertTrue(hasattr(secrets, 'RetainedSecrets'), 'public prepared retention port is missing')
        initialize()
        project = Project.objects.create(name='alpha', origin='https://example.invalid/alpha.git')
        scope = secrets.RetentionScope(project.pk, 1, uuid.uuid4(), 'policy')
        class Authority:
            def check(self, actor, actual):
                if actor != 'root' or actual != scope:
                    raise PermissionError('outside pinned retention scope')
        provider = XChaCha20Provider(Keyring('test', {'test': b'q' * 32}))
        store = secrets.RetainedSecrets(provider=provider, authority=Authority())
        prepared = store.prepare('root', scope, b'private retained bytes')
        self.assertNotIn('private retained bytes', repr(prepared))
        with transaction.atomic():
            version = store.commit('root', prepared)
        self.assertEqual(store.read('root', scope, version), b'private retained bytes')
        with self.assertRaises(PermissionError):
            store.read('root', replace(scope, subject_id=uuid.uuid4()), version)
        with self.assertRaises(PermissionError):
            secrets.RetainedSecrets(provider=provider).prepare('root', scope, b'no')
        with transaction.atomic():
            with self.assertRaises(ValueError):
                store.prepare('root', scope, b'no provider inside SQL')


class GrantCases(TransactionTestCase):
    # Как остальные ORM cases: flush не удаляет seeds новой установки.
    serialized_rollback = True

    def setUp(self):
        self.assertTrue(hasattr(access, 'RoleBundle'), 'immutable ORM RoleBundle is missing')
        self.assertTrue(hasattr(access, 'Grant'), 'ORM delegated grant lifecycle is missing')
        self.g = importlib.import_module('mop.server.access.grants')
        initialize()
        self.project = Project.objects.create(name='alpha', origin='https://example.invalid/alpha.git')
        self.foreign = Project.objects.create(name='beta', origin='https://example.invalid/beta.git')
        self.source = access.IdentitySource.objects.create(name='local', kind='local')
        self.user = get_user_model().objects.create_user('owner', password='test-only-password')
        access.Principal.objects.create(user=self.user, kind='human')
        access.LoginIdentity.objects.create(user=self.user, source=self.source, external_subject='owner')
        access.ProjectMembership.objects.create(user=self.user, project=self.project, source=self.source)
        self.permit('access.view_role', 'access.approve_role', 'access.issue_grant', 'access.revoke_grant',
                    'catalog.view_pool', 'secrets.manage_project_secret')
        self.actor = auth.token_context(auth.issue_token(self.user.pk))
        self.provider = XChaCha20Provider(Keyring('test', {'test': b'q' * 32}))
        from mop.server.secrets.services import SecretStore
        policy = SecretStore(provider=self.provider, authorizer=auth.Access()).create(self.actor,
            project_id=self.project.pk, name='approved-policy', kind='credential', purpose='policy', value=b'exact approved bytes')
        self.proof = self.g.ApprovedPolicy(uuid.uuid4(), policy.active_version_id, 'a' * 64,
                                           self.user.pk, self.project.pk)
        self.approvals = ApprovalFixture(self.proof)
        self.bindings = BindingFixture()
        self.api = self.g.Grants(provider=self.provider, approvals=self.approvals, bindings=self.bindings)
        # Будущие владельцы permissions ещё не имеют apps. Регистрируем только
        # стандартные Django Permission, не ослабляем production vocabulary.
        from django.contrib.contenttypes.models import ContentType
        ct, _ = ContentType.objects.get_or_create(app_label='sessions', model='grant_test_fixture')
        for code in ('create_puppet', 'send_message', 'dispatch_task'):
            Permission.objects.get_or_create(content_type=ct, codename=code)
            self.permit('sessions.' + code)
        self.bundle = self.publish()

    def permit(self, *actions):
        for action in actions:
            app, code = action.split('.')
            self.user.user_permissions.add(Permission.objects.get(content_type__app_label=app, codename=code))

    def publish(self, role='worker', **options):
        raw = ('# exact manifest bytes\n' + json.dumps(manifest(role, **options), ensure_ascii=False)).encode()
        value = self.api.publish_bundle(self.actor, project_id=self.project.pk,
            manifest=raw, prompt=b'private role prompt\r\n', provenance='test source revision')
        self.api.approve_bundle(self.actor, value.id)
        return value

    def reserve(self, *, bundle=None, actor=None, request_id=None, **kw):
        args = dict(project_id=self.project.pk, bundle_id=(bundle or self.bundle).id,
                    target_id=uuid.uuid4(), incarnation=uuid.uuid4(), request_id=request_id or uuid.uuid4(),
                    expires_at=timezone.now() + timedelta(hours=1))
        args.update(kw)
        return self.api.reserve(actor or self.actor, **args)

    def active(self, *, bundle=None, actor=None):
        row = self.reserve(bundle=bundle, actor=actor)
        self.api.provision(actor or self.actor, row.id, expected_revision=row.revision)
        row = self.api.get(actor or self.actor, row.id)
        binding = self.g.RoleBinding(self.project.pk, row.target_id, row.incarnation,
                                    row.role, row.digest, row.id, self.user.pk)
        self.bindings.bindings[(self.project.pk, row.target_id)] = binding
        self.bindings.receipts[row.id] = binding
        self.api.activate(row.id, expected_revision=row.revision)
        row = self.api.get(self.actor, row.id)
        token = Token.objects.get(user__principal__grant__id=row.id).key
        return row, auth.token_context(token, grants=self.api)

    def race(self, fn):
        barrier = Barrier(2)
        def run(i):
            try:
                barrier.wait(timeout=20)
                return fn(i)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(run, (0, 1)))

    def test_exact_bytes_canonical_digest_normalized_caps_and_immutable_write_paths(self):
        value = self.api.read_bundle(self.actor, self.bundle.id)
        self.assertEqual(value.prompt, b'private role prompt\r\n')
        self.assertTrue(value.manifest.startswith(b'# exact manifest bytes\n'))
        parsed = soul.parse(value.manifest.decode())
        self.assertEqual(self.bundle.digest, soul.digest(parsed.to_dict(), value.prompt))
        self.assertIsInstance(value.capabilities.node_verbs, tuple)
        model = access.RoleBundle.objects.get(pk=self.bundle.id)
        for mutate in (lambda: access.RoleBundle.objects.filter(pk=model.pk).update(digest='b' * 64),
                       lambda: access.RoleBundle.objects.bulk_update([model], ['digest']),
                       lambda: access.RoleBundle.objects.bulk_create([model]), lambda: model.save()):
            with self.assertRaises(ValueError):
                mutate()
        dump = serializers.serialize('json', access.RoleBundle.objects.all())
        self.assertNotIn('private role prompt', dump)
        self.assertNotIn('private role prompt', repr(value))
        self.assertEqual(self.api.read_bundle(self.actor, model.pk), value)

    def test_bundle_is_not_self_approved_and_owner_checks_every_read(self):
        other = get_user_model().objects.create_user('other', password='test')
        auth.bind_local_identity(other.pk)
        other.user_permissions.set(self.user.user_permissions.all())
        access.ProjectMembership.objects.create(user=other, project=self.project, source=self.source)
        actor = auth.token_context(auth.issue_token(other.pk))
        with self.assertRaises(PermissionError):
            self.api.read_bundle(actor, self.bundle.id)
        self.user.user_permissions.clear()
        with self.assertRaises(PermissionError):
            self.api.read_bundle(self.actor, self.bundle.id)

    def test_role_binding_rejects_partial_or_noncanonical_triple(self):
        for values in (('', 'a' * 64, uuid.uuid4()), ('worker', '', uuid.uuid4()), ('worker', 'bad', None)):
            with self.assertRaises(ValueError):
                self.g.RoleBinding(self.project.pk, uuid.uuid4(), values[2], values[0], values[1], uuid.uuid4(), self.user.pk)

    def test_reservation_retry_is_same_child_changed_intent_conflicts_without_repin(self):
        args = dict(target_id=uuid.uuid4(), incarnation=uuid.uuid4(), request_id=uuid.uuid4(),
                    expires_at=timezone.now() + timedelta(hours=1))
        first = self.reserve(**args)
        self.approvals.proof = replace(self.proof, approval_id=uuid.uuid4())
        second = self.reserve(**args)
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.approvals.calls, 1)
        self.assertEqual(second.policy, self.proof)
        for field in ('target_id', 'incarnation'):
            with self.assertRaises(self.g.Conflict):
                self.reserve(**{**args, field: uuid.uuid4()})
        self.assertEqual(access.Grant.objects.count(), 1)

    def test_concurrent_identical_reservation_has_one_child_and_one_policy_pin(self):
        args = dict(target_id=uuid.uuid4(), incarnation=uuid.uuid4(), request_id=uuid.uuid4(),
                    expires_at=timezone.now() + timedelta(hours=1))
        rows = self.race(lambda i: self.reserve(**args))
        self.assertEqual(rows[0].id, rows[1].id)
        self.assertEqual(access.Grant.objects.count(), 1)
        self.assertEqual(self.approvals.calls, 1)

    def test_reserved_has_no_runtime_authority_and_default_ports_deny(self):
        row = self.reserve()
        self.api.provision(self.actor, row.id, expected_revision=row.revision)
        token = Token.objects.get(user__principal__grant__id=row.id).key
        with self.assertRaises(PermissionError):
            auth.token_context(token, grants=self.api)
        with self.assertRaises(PermissionError):
            self.api.activate(row.id, expected_revision=self.api.get(self.actor, row.id).revision)
        with self.assertRaises(PermissionError):
            self.g.Grants(provider=self.provider).reserve(self.actor, project_id=self.project.pk,
                bundle_id=self.bundle.id, target_id=uuid.uuid4(), incarnation=uuid.uuid4(),
                request_id=uuid.uuid4(), expires_at=timezone.now() + timedelta(hours=1))

    def test_real_provider_provisions_encrypted_token_without_human_secret_management(self):
        permission = Permission.objects.get(content_type__app_label='secrets', codename='manage_project_secret')
        self.user.user_permissions.remove(permission)
        row, actor = self.active()
        user = get_user_model().objects.get(pk=actor.user_id)
        self.assertFalse(user.is_staff or user.is_superuser or user.has_usable_password())
        self.assertEqual((actor.human_owner_id, actor.grant_id), (self.user.pk, row.id))
        self.assertIsNotNone(row.credential_version_id)
        key = Token.objects.get(user=user).key
        dump = serializers.serialize('json', access.Grant.objects.all())
        self.assertNotIn(key, dump + repr(row))
        from mop.server.secrets.models import SecretVersion
        encrypted = SecretVersion.objects.get(pk=row.credential_version_id)
        self.assertNotIn(key.encode(), bytes(encrypted.envelope))
        self.assertFalse(hasattr(row, 'token'))

    # BUG: trusted view resolver проходит permission, но Identity повторяет default-deny lookup.
    # HYPOTHESIS: handler заново создаёт context без resolver вместо request.actor.
    # RED: PG16/17 вернули 403 delegation resolver is not bound вместо 200.
    # SOLUTION: revalidate request.actor тем же trusted view resolver, grant_id из context.
    # RESULT: PG16/17 203/203; активный grant принят, отсутствие resolver/отзыв отказаны.
    # STATUS: FIXED — see #635.
    def test_http_identity_uses_accepted_grant_context_and_rechecks_revocation(self):
        from mop.server.controller.authviews import Identity
        from rest_framework.test import APIRequestFactory
        row, actor = self.active()
        key = Token.objects.get(user_id=actor.user_id).key
        view = type('BoundIdentity', (Identity,), {'grant_resolver': self.api}).as_view()
        factory = APIRequestFactory()
        response = view(factory.get('/identity', HTTP_AUTHORIZATION='Token ' + key))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['kind'], 'grant')
        self.assertEqual(response.data['principal_id'], str(actor.principal_id))
        self.assertEqual(response.data['human_owner_id'], self.user.pk)
        self.assertEqual(response.data['grant_id'], str(row.id))
        # Никакой настройки из payload: без trusted root прежний deny обязателен.
        unbound = Identity.as_view()(factory.get('/identity?grant_resolver=accepted',
                                                HTTP_AUTHORIZATION='Token ' + key))
        self.assertEqual(unbound.status_code, 403)
        self.api.revoke(self.actor, row.id, expected_revision=row.revision)
        refused = view(factory.get('/identity', HTTP_AUTHORIZATION='Token ' + key))
        self.assertEqual(refused.status_code, 403)

    def test_grant_auth_default_denies_node_is_not_grant_and_token_revoke_is_live(self):
        row, actor = self.active()
        key = Token.objects.get(user_id=actor.user_id).key
        with self.assertRaises(PermissionError):
            auth.token_context(key)
        self.api.authorize(actor, 'sessions.send_message', project_ids=(self.project.pk,))
        Token.objects.filter(user_id=actor.user_id).delete()
        with self.assertRaises(PermissionError):
            self.api.resolve_actor(actor)
        with self.assertRaises(PermissionError):
            self.api.resolve_actor(self.actor)

    def test_owner_permission_membership_and_deactivation_are_current(self):
        row, actor = self.active()
        self.api.authorize(actor, 'sessions.send_message', project_ids=(self.project.pk,))
        permission = Permission.objects.get(content_type__app_label='sessions', codename='send_message')
        self.user.user_permissions.remove(permission)
        with self.assertRaises(PermissionError):
            self.api.authorize(actor, 'sessions.send_message', project_ids=(self.project.pk,))
        self.user.user_permissions.add(permission)
        access.ProjectMembership.objects.filter(user=self.user).delete()
        with self.assertRaises(PermissionError):
            self.api.resolve_actor(actor)
        access.ProjectMembership.objects.create(user=self.user, project=self.project, source=self.source)
        self.user.is_active = False
        self.user.save()
        with self.assertRaises(PermissionError):
            self.api.resolve_actor(actor)

    def test_same_project_plain_send_is_not_dispatch_or_human_override(self):
        row, actor = self.active()
        auth.Access(grants=self.api).authorize(actor, 'sessions.send_message', project_ids=(self.project.pk,))
        with self.assertRaises(PermissionError):
            self.api.authorize(actor, 'sessions.dispatch_task', project_ids=(self.project.pk,))
        with self.assertRaises(PermissionError):
            self.api.authorize(actor, 'sessions.send_message', project_ids=(self.foreign.pk,))
        with self.assertRaises(PermissionError):
            self.api.authorize(actor, 'operations.override_assignment', project_ids=(self.project.pk,))
        with self.assertRaises(PermissionError):
            self.api.authorize(actor, 'secrets.manage_project_secret', project_ids=(self.project.pk,))

    def test_dispatch_requires_source_target_binding_and_current_owner_identity(self):
        dispatcher = self.publish('dispatcher', dispatch=True)
        source, actor = self.active(bundle=dispatcher)
        target, _ = self.active()
        decision = self.api.authorize_dispatch(actor, target_id=target.target_id)
        self.assertEqual(decision.human_owner_id, self.user.pk)
        self.assertTrue(decision.claim_assignment)
        plain = self.api.authorize_message(actor, target_id=target.target_id)
        self.assertFalse(plain.claim_assignment)
        self.assertIsNone(plain.git_identity_owner_id)
        self.bindings.bindings[(self.project.pk, target.target_id)] = replace(
            self.bindings.bindings[(self.project.pk, target.target_id)], incarnation=uuid.uuid4())
        with self.assertRaises(PermissionError):
            self.api.authorize_dispatch(actor, target_id=target.target_id)

    def test_spawn_allowlist_pinned_policy_and_revoked_ancestor(self):
        parent_bundle = self.publish('parent', spawn=('worker',))
        parent, actor = self.active(bundle=parent_bundle)
        child = self.reserve(actor=actor)
        self.assertEqual(child.parent_id, parent.id)
        self.assertEqual(child.policy, parent.policy)
        with self.assertRaises(PermissionError):
            self.reserve(actor=actor, bundle=parent_bundle)
        self.api.revoke(self.actor, parent.id, expected_revision=parent.revision)
        with self.assertRaises(PermissionError):
            self.api.provision(actor, child.id, expected_revision=child.revision)

    def test_policy_revocation_blocks_spawn_without_replacing_pinned_policy(self):
        parent, actor = self.active(bundle=self.publish('parent', spawn=('worker',)))
        self.approvals.enabled = False
        with self.assertRaises(PermissionError):
            self.reserve(actor=actor)
        with self.assertRaises(PermissionError):
            self.api.resolve_actor(actor)

    def test_revocation_and_expiry_are_terminal_stale_activation_cannot_resurrect(self):
        row = self.reserve()
        revoked = self.api.revoke(self.actor, row.id, expected_revision=row.revision)
        self.assertEqual(revoked.state, 'revoked')
        with self.assertRaises(self.g.Conflict):
            self.api.activate(row.id, expected_revision=row.revision)
        self.assertEqual(self.api.get(self.actor, row.id).state, 'revoked')
        with self.assertRaises(ValueError):
            self.reserve(expires_at=timezone.now() - timedelta(seconds=1))

    def test_new_incarnation_rejects_late_old_activation_receipt(self):
        row = self.reserve()
        self.api.provision(self.actor, row.id, expected_revision=row.revision)
        current = self.api.get(self.actor, row.id)
        old = self.g.RoleBinding(self.project.pk, row.target_id, row.incarnation,
                                row.role, row.digest, row.id, self.user.pk)
        self.bindings.receipts[row.id] = old
        self.bindings.bindings[(self.project.pk, row.target_id)] = replace(old, incarnation=uuid.uuid4())
        with self.assertRaises(PermissionError):
            self.api.activate(row.id, expected_revision=current.revision)

    def test_corrupt_normalized_bundle_and_digest_deny_all_shared_reads(self):
        row, actor = self.active()
        # DBA/raw repair не trust boundary, но чтение обязано обнаружить corruption.
        access.RoleBundle._base_manager.filter(pk=self.bundle.id).update(digest='b' * 64)
        for read in (lambda: self.api.read_bundle(self.actor, self.bundle.id),
                     lambda: self.api.get(self.actor, row.id), lambda: self.api.resolve_actor(actor)):
            with self.assertRaises(PermissionError):
                read()

    def test_unapproved_bundle_cannot_reserve_and_grant_cannot_approve(self):
        unapproved = self.api.publish_bundle(self.actor, project_id=self.project.pk,
            manifest=json.dumps(manifest('unapproved')).encode(), prompt=b'new', provenance='unapproved')
        with self.assertRaises(PermissionError):
            self.reserve(bundle=unapproved)
        row, actor = self.active()
        with self.assertRaises(PermissionError):
            self.api.approve_bundle(actor, self.bundle.id)

    def test_false_boolean_is_not_an_approval_proof(self):
        class BooleanApproval:
            def pin(self, binding):
                return True
        api = self.g.Grants(provider=self.provider, approvals=BooleanApproval(), bindings=self.bindings)
        with self.assertRaises(PermissionError):
            api.reserve(self.actor, project_id=self.project.pk, bundle_id=self.bundle.id,
                target_id=uuid.uuid4(), incarnation=uuid.uuid4(), request_id=uuid.uuid4(),
                expires_at=timezone.now() + timedelta(hours=1))

    def test_concurrent_revoke_activate_has_terminal_revocation(self):
        row = self.reserve()
        self.api.provision(self.actor, row.id, expected_revision=row.revision)
        row = self.api.get(self.actor, row.id)
        binding = self.g.RoleBinding(self.project.pk, row.target_id, row.incarnation,
                                    row.role, row.digest, row.id, self.user.pk)
        self.bindings.bindings[(self.project.pk, row.target_id)] = binding
        self.bindings.receipts[row.id] = binding
        def work(i):
            try:
                if i:
                    return self.api.activate(row.id, expected_revision=row.revision).state
                return self.api.revoke(self.actor, row.id, expected_revision=row.revision).state
            except (self.g.Conflict, PermissionError):
                return 'conflict'
        states = self.race(work)
        self.assertIn('conflict', states)
        current = self.api.get(self.actor, row.id)
        if current.state == 'active':
            current = self.api.revoke(self.actor, row.id, expected_revision=current.revision)
        self.assertEqual(current.state, 'revoked')
        with self.assertRaises(self.g.Conflict):
            self.api.activate(row.id, expected_revision=row.revision)

    def test_current_expiry_service_user_disable_and_ancestor_token_revoke_deny(self):
        row, actor = self.active()
        access.CreationIntent._base_manager.filter(pk=access.Grant.objects.get(pk=row.id).intent_id).update(
            expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(PermissionError):
            self.api.resolve_actor(actor)
        expired = self.api.expire(row.id, expected_revision=row.revision)
        self.assertEqual(expired.state, 'expired')
        with self.assertRaises(ValueError):
            self.api.revoke(self.actor, row.id, expected_revision=expired.revision)
        parent, parent_actor = self.active(bundle=self.publish('parent', spawn=('worker',)))
        child, child_actor = self.active(actor=parent_actor)
        Token.objects.filter(user_id=parent_actor.user_id).delete()
        with self.assertRaises(PermissionError):
            self.api.resolve_actor(child_actor)

    def test_dispatch_target_refusal_and_ancestor_revocation_apply_to_delivery(self):
        source, actor = self.active(bundle=self.publish('dispatcher', dispatch=True))
        target, target_actor = self.active(bundle=self.publish('no-dispatch', receives=False))
        self.api.authorize_message(actor, target_id=target.target_id)
        with self.assertRaises(PermissionError):
            self.api.authorize_dispatch(actor, target_id=target.target_id)
        Token.objects.filter(user_id=target_actor.user_id).delete()
        with self.assertRaises(PermissionError):
            self.api.authorize_message(actor, target_id=target.target_id)

    def test_corrupt_retained_bytes_deny_activation_before_state_change(self):
        row = self.reserve()
        self.api.provision(self.actor, row.id, expected_revision=row.revision)
        row = self.api.get(self.actor, row.id)
        binding = row.binding
        self.bindings.bindings[(self.project.pk, row.target_id)] = binding
        self.bindings.receipts[row.id] = binding
        from mop.server.secrets.models import SecretVersion
        version_id = access.RoleBundle.objects.get(pk=self.bundle.id).content_version_id
        version = SecretVersion.objects.get(pk=version_id)
        encrypted = bytes(version.envelope)
        SecretVersion._base_manager.filter(pk=version_id).update(envelope=encrypted[:-1] + bytes([encrypted[-1] ^ 1]))
        with self.assertRaises(PermissionError):
            self.api.activate(row.id, expected_revision=row.revision)
        self.assertEqual(access.Grant.objects.get(pk=row.id).state, 'reserved')

    def test_prepared_credential_roundtrip_and_plaintext_free_real_pg_dump(self):
        row = self.reserve()
        provisioned = self.api.provision(self.actor, row.id, expected_revision=row.revision)
        token = Token.objects.get(user__principal__grant__id=row.id).key
        from mop.server.secrets.services import RetentionScope
        self.assertEqual(self.api.secrets.read(self.actor, RetentionScope(self.project.pk, self.user.pk,
            row.id, 'grant'), provisioned.credential_version_id), token.encode())
        import tempfile
        from mop.server.controller.backup import _tool
        with tempfile.TemporaryFile() as raw:
            db = connections['default']
            _tool('pg_dump', db.settings_dict, major=db.pg_version // 10000,
                  output=raw, extra=('--data-only', '--table=access_*', '--table=secrets_*'))
            raw.seek(0)
            data = raw.read()
        self.assertNotIn(token.encode(), data)
        self.assertNotIn(b'private role prompt', data)
        self.assertNotIn(token, serializers.serialize('json', access.GrantEvent.objects.all()))

    def test_transport_retirement_is_separate_and_unknown_does_not_restore_access(self):
        row, actor = self.active()
        revoked = self.api.revoke(self.actor, row.id, expected_revision=row.revision)
        class ReceiptBindings(BindingFixture):
            def retirement(inner, grant_id, revision):
                return self.g.RetirementReceipt(row.binding, revision, 'unknown')
        self.api.bindings = ReceiptBindings()
        result = self.api.record_retirement(row.id, expected_revision=revoked.revision)
        self.assertEqual((result.state, result.retirement), ('revoked', 'unknown'))
        self.assertTrue(Token.objects.filter(user_id=actor.user_id).exists())
        with self.assertRaises(PermissionError):
            self.api.resolve_actor(actor)

    def test_provision_database_failure_is_atomic_and_does_not_leak_key(self):
        from unittest.mock import patch
        from django.db import IntegrityError
        row = self.reserve()
        with patch.object(access.CredentialBinding.objects, 'create', side_effect=IntegrityError('private-generated-key')):
            try:
                self.api.provision(self.actor, row.id, expected_revision=row.revision)
            except Exception as error:
                self.assertNotIn('private-generated-key', str(error))
            else:
                self.fail('failed credential binding must refuse provisioning')
        self.assertFalse(Token.objects.filter(user__principal__grant__id=row.id).exists())
        self.assertIsNone(access.Grant.objects.get(pk=row.id).credential_version_id)

    def test_bundle_read_of_revoked_descendant_is_not_a_shared_read_bypass(self):
        parent, parent_actor = self.active(bundle=self.publish('parent', spawn=('worker',)))
        child, child_actor = self.active(actor=parent_actor)
        self.api.revoke(self.actor, parent.id, expected_revision=parent.revision)
        with self.assertRaises(PermissionError):
            self.api.read_bundle(child_actor, self.bundle.id)

    def test_approval_port_receives_exact_stored_intent_binding(self):
        class BoundApproval:
            def __init__(inner):
                inner.pins, inner.validations = [], []
            def pin(inner, binding):
                inner.pins.append(binding)
                return self.proof
            def validate(inner, proof, binding):
                inner.validations.append((proof, binding))
        approved = BoundApproval()
        self.api.approvals = approved
        row = self.reserve()
        intent = access.CreationIntent.objects.get(grant__id=row.id)
        binding = approved.pins[0]
        self.assertEqual((binding.intent_id, binding.target_id, binding.incarnation, binding.bundle_id,
                          binding.owner_id, binding.actor_id, binding.project_id),
                         (intent.id, row.target_id, row.incarnation, self.bundle.id,
                          self.user.pk, self.actor.principal_id, self.project.pk))
        self.assertEqual(approved.validations[-1], (row.policy, binding))

    def test_http_auth_injection_is_explicit_and_no_request_owner_is_used(self):
        row, actor = self.active()
        from types import SimpleNamespace
        from mop.server.controller.authentication import actor_for_request
        request = SimpleNamespace(user=get_user_model().objects.get(pk=actor.user_id),
            auth=Token.objects.get(user_id=actor.user_id), data={'owner': 'forged', 'force': True})
        self.assertEqual(actor_for_request(request, grants=self.api), actor)
        with self.assertRaises(PermissionError):
            actor_for_request(request)

    def test_revoke_during_target_provider_wait_denies_before_delivery_effect(self):
        source, actor = self.active()
        target, _ = self.active(bundle=self.publish('target-role'))
        target_version = access.RoleBundle.objects.get(pk=target.bundle_id).content_version_id
        provider = self.provider
        class RevokeDuringDecrypt:
            fired = False
            def encrypt(inner, value, context):
                return provider.encrypt(value, context)
            def decrypt(inner, envelope, context):
                value = provider.decrypt(envelope, context)
                if context.record == target_version.bytes and not inner.fired:
                    inner.fired = True
                    self.api.revoke(self.actor, source.id, expected_revision=source.revision)
                return value
        class Delivery:
            calls = []
            def send(inner, decision, message):
                inner.calls.append(decision)
        self.api.secrets.provider = RevokeDuringDecrypt()
        self.api.messages = Delivery()
        with self.assertRaises(PermissionError):
            self.api.send(actor, target_id=target.target_id, message=b'not delivered')
        self.assertEqual(self.api.messages.calls, [])

    def test_grant_cannot_read_an_unrelated_owner_bundle_prompt(self):
        row, actor = self.active()
        private = self.publish('not-delegated')
        with self.assertRaises(PermissionError):
            self.api.read_bundle(actor, private.id)
        self.assertEqual(self.api.read_bundle(actor, row.bundle_id).digest, row.digest)

    def test_child_bundle_cannot_expand_parent_secret_allowlist(self):
        parent, actor = self.active(bundle=self.publish('parent', spawn=('sensitive',)))
        doc = manifest('sensitive')
        doc['secrets'] = {'files': ['not-approved-for-parent'], 'vars': []}
        bundle = self.api.publish_bundle(self.actor, project_id=self.project.pk,
            manifest=json.dumps(doc).encode(), prompt=b'prompt', provenance='test')
        self.api.approve_bundle(self.actor, bundle.id)
        with self.assertRaises(PermissionError):
            self.reserve(actor=actor, bundle=bundle)

    def test_crypto_provider_never_runs_in_sql_and_no_unsupported_effect_port(self):
        with transaction.atomic():
            with self.assertRaises(ValueError):
                self.publish('another')
        row, actor = self.active()
        with self.assertRaises(PermissionError):
            self.api.send(actor, target_id=row.target_id, message=b'private message')
