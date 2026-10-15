"""Контракт каталога #634 на настоящем PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import hashlib
import threading
from unittest.mock import patch

from django.db import DatabaseError, IntegrityError, connections, transaction
from django.db.models.deletion import ProtectedError
from django.test import TransactionTestCase
from django.utils import timezone

from mop.server.catalog import models, services
from mop.server.controller.runtime import initialize

# HYPOTHESIS: чистая policy не обеспечивает durable identity, CAS и receipts.
# Проверки требуют собственные модели/API явно, а не падают на чужом import.
# SOLUTION: короткие ORM use cases с trusted портами, без прежних readers.
class Allow:
    def __init__(self, projects=None):
        self.projects = projects
        self.calls = []

    def authorize(self, actor, action, *, project_ids=(), node_ids=(), client_ids=()):
        self.calls.append((actor, action, tuple(project_ids), tuple(node_ids), tuple(client_ids)))
        if self.projects is not None and any(p not in self.projects for p in project_ids):
            raise PermissionError('out of scope')


# BUG: non-superuser не может получить незарегистрированные custom actions.
# HYPOTHESIS: post_migrate создаёт только default permissions; limit выдаётся как change_project.
class CatalogPermissionCases(TransactionTestCase):
    def user(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(username='catalog-service')
        user.set_unusable_password()
        user.save()
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        return user

    def grant(self, user, *codenames):
        from django.contrib.auth.models import Group, Permission
        group, _ = Group.objects.get_or_create(name='catalog-test-operator')
        for code in codenames:
            rows = Permission.objects.filter(content_type__app_label='catalog', codename=code)
            self.assertEqual(rows.count(), 1, f'post_migrate did not create exactly one assignable catalog.{code}')
            group.permissions.add(rows.get())
        user.groups.add(group)
        return group

    def authorizer(self):
        # Только action check для теста каталога, не scope/grant evaluator #633.
        from catalog_schema import permission_registration_contract
        permission_registration_contract()
        from mop.server.catalog.permissions import ACTION_PERMISSIONS
        class DjangoPermissions:
            def authorize(self, actor, action, **refs):
                # Новый instance: Django ModelBackend кэширует права на User.
                current = type(actor).objects.get(pk=actor.pk)
                if not current.has_perms(ACTION_PERMISSIONS[action]):
                    raise PermissionError('missing catalog action permissions')
        return DjangoPermissions()

    def test_default_post_migrate_creates_assignable_custom_and_model_permissions(self):
        from django.apps import apps
        from django.contrib.auth.models import Permission
        from catalog_schema import CUSTOM_PERMISSIONS
        for model in apps.get_app_config('catalog').get_models():
            opts = model._meta
            expected = {f'{verb}_{opts.model_name}' for verb in opts.default_permissions}
            expected |= CUSTOM_PERMISSIONS.get(opts.model_name, set())
            actual = list(Permission.objects.filter(content_type__app_label='catalog',
                content_type__model=opts.model_name).values_list('codename', flat=True))
            with self.subTest(model=opts.model_name):
                self.assertCountEqual(actual, expected)

    def test_limit_emits_distinct_action_with_real_project_scope(self):
        row = models.Project.objects.create(origin='https://git.example/alpha.git', name='alpha')
        auth = Allow()
        services.Catalog(authorizer=auth).set_project_limit('test', row.pk, 2, expected_revision=1)
        self.assertEqual(auth.calls[-1][1:4], ('catalog.set_project_limit', (row.pk,), ()))

    def test_non_superuser_limit_requires_every_permission_not_origin_change_alone(self):
        user = self.user()
        row = models.Project.objects.create(origin='https://git.example/alpha.git', name='alpha')
        api = services.Catalog(authorizer=self.authorizer())
        required = ('change_project', 'configure_project', 'set_project_limit')
        group = self.grant(user, *required)
        from django.contrib.auth.models import Permission
        for missing in required:
            permission = Permission.objects.get(content_type__app_label='catalog', codename=missing)
            group.permissions.remove(permission)
            with self.subTest(missing=missing), self.assertRaises(PermissionError):
                api.set_project_limit(user, row.pk, 5, expected_revision=1)
            row.refresh_from_db()
            self.assertEqual((row.puppet_limit, row.revision), (None, 1))
            group.permissions.add(permission)
        group.permissions.remove(Permission.objects.get(content_type__app_label='catalog', codename='set_project_limit'))
        origin = api.replace_project_origin(user, row.pk, 'https://git.example/beta.git', expected_revision=1)
        self.assertEqual(origin.name, 'beta')
        with self.assertRaises(PermissionError):
            api.set_project_limit(user, row.pk, 5, expected_revision=origin.revision)
        self.grant(user, 'set_project_limit')
        result = api.set_project_limit(user, row.pk, 5, expected_revision=origin.revision)
        self.assertEqual((result.puppet_limit, result.revision), (5, origin.revision + 1))

    def test_machine_observation_and_application_permissions_are_independently_assignable(self):
        initialize()
        user = self.user()
        row = models.Node.objects.create(name='machine', driver='host', ssh_host='ssh.example',
            control_origin='https://control.example', lan_address='10.0.0.2', bootstrap_address='boot.example')
        api = services.Catalog(authorizer=self.authorizer())
        actions = {
            'observe_node': lambda: api.observe_node(user, row.pk, eligible=True,
                source='scheduler:test', observed_at=timezone.now()),
            'record_node_application': lambda: api.record_node_application(user, row.pk, target_revision=1, receipt='node:1'),
            'record_settings_application': lambda: api.record_settings_application(user, target_revision=1, receipt='settings:1'),
        }
        for code, operation in actions.items():
            with self.subTest(action=code):
                with self.assertRaises(PermissionError):
                    operation()
                self.grant(user, code)
                self.assertTrue(operation())
        self.assertTrue(api.record_settings_application(user, node_id=row.pk, target_revision=1, receipt='node-settings:1'))
        row.refresh_from_db()
        self.assertEqual((row.applied_receipt, row.settings_receipt, row.observed_eligible), ('node:1', 'node-settings:1', True))
        with self.assertRaises(PermissionError):
            api.set_node_eligibility(user, row.pk, False, expected_revision=1)

    def test_policy_pin_permission_is_assignable_without_model_change_or_build(self):
        user = self.user()
        row = models.Project.objects.create(origin='https://git.example/alpha.git', name='alpha')
        api = services.Catalog(authorizer=self.authorizer())
        def pin():
            return api.pin_project_policy(user, row.pk, b'policy', source_commit='a'*40,
                source_ref='main', provenance='test', cpu=100, memory_mb=256, disk_mb=1024, expected_revision=1)
        with self.assertRaises(PermissionError):
            pin()
        self.grant(user, 'pin_project_policy')
        self.assertEqual(pin().content, b'policy')
        with self.assertRaises(PermissionError):
            api.replace_project_origin(user, row.pk, 'https://git.example/beta.git', expected_revision=2)


class CatalogCases(TransactionTestCase):
    def setUp(self):
        self.assertTrue(hasattr(services, 'Catalog'), 'missing catalogue domain API: Catalog')
        for name in ('Project', 'Node', 'NodeProject', 'InstallationSetting', 'NodeSetting',
                     'ProjectPolicySnapshot', 'ImageRevision', 'NodeImage',
                     'ClientRegistration', 'WorkspaceBinding'):
            self.assertTrue(hasattr(models, name), 'missing catalogue domain model: ' + name)
        initialize()
        self.auth = Allow()
        self.api = services.Catalog(authorizer=self.auth)
        self.actor = 'trusted-test-actor'

    def project(self, name='alpha'):
        return self.api.register_project(self.actor, 'https://git.example/team/' + name + '.git')

    def node(self, name='node-a'):
        return self.api.configure_node(self.actor, name=name, driver='host', ssh_host='ssh.example',
            ssh_port=2222, control_origin='https://control.example', lan_address='10.20.0.2',
            bootstrap_address='bootstrap.example')

    def race(self, operation):
        barrier = threading.Barrier(2)
        def worker(i):
            try:
                barrier.wait(timeout=5)
                return operation(i)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(worker, range(2)))

    def test_same_origin_concurrent_creation_has_one_identity(self):
        results = self.race(lambda _: self.project())
        self.assertEqual(results[0].id, results[1].id)
        self.assertEqual(models.Project.objects.count(), 1)
        self.assertEqual(results[0].name, 'alpha')
        self.assertEqual(results[0].revision, 1)
        with self.assertRaises(FrozenInstanceError):
            results[0].name = 'forged'

    def test_same_name_different_origins_is_concurrent_conflict(self):
        def create(i):
            try:
                self.api.register_project(self.actor, f'https://git.example/team{i}/alpha.git')
                return 'created'
            except services.Conflict:
                return 'conflict'
        self.assertCountEqual(self.race(create), ['created', 'conflict'])
        self.assertEqual(models.Project.objects.count(), 1)

    def test_limits_and_origin_replacement_use_policy_and_cas(self):
        p = self.project()
        for invalid in (-1, True, False, 1.5, '2'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.api.set_project_limit(self.actor, p.id, invalid, expected_revision=p.revision)
        for value in (0, 2, None):
            p = self.api.set_project_limit(self.actor, p.id, value, expected_revision=p.revision)
            self.assertEqual(p.puppet_limit, value)
        q = self.api.replace_project_origin(self.actor, p.id, 'ssh://git@git.example/team/beta.git',
                                             expected_revision=p.revision)
        self.assertEqual((q.id, q.name), (p.id, 'beta'))
        with self.assertRaises(services.Conflict):
            self.api.set_project_limit(self.actor, p.id, 3, expected_revision=p.revision)
        with self.assertRaises(ValueError):
            self.api.register_project(self.actor, 'https://user:secret@git.example/repo.git')
        self.project('taken')
        with self.assertRaises(services.Conflict):
            self.api.replace_project_origin(self.actor, q.id, 'https://else.example/taken.git', expected_revision=q.revision)
        self.assertEqual(self.api.get_project(self.actor, q.id), q)
        with self.assertRaises(IntegrityError), transaction.atomic():
            models.Project.objects.filter(pk=q.id).update(puppet_limit=-1)

    def test_same_revision_writers_have_exactly_one_winner(self):
        p = self.project()
        def change(i):
            try:
                self.api.set_project_limit(self.actor, p.id, i, expected_revision=1)
                return 'changed'
            except services.Conflict:
                return 'conflict'
        self.assertCountEqual(self.race(change), ['changed', 'conflict'])
        self.assertEqual(self.api.get_project(self.actor, p.id).revision, 2)

    def test_nodes_preserve_address_roles_and_receipts_never_apply_newer_intent(self):
        n = self.node()
        self.assertEqual((n.ssh_port, n.control_origin, n.lan_address, n.bootstrap_address),
                         (2222, 'https://control.example', '10.20.0.2', 'bootstrap.example'))
        self.assertIsNone(n.applied_revision)
        self.assertIsNone(n.observed_eligible)
        n2 = self.api.set_deployment_membership(self.actor, n.id, True, expected_revision=n.revision)
        n3 = self.api.set_node_eligibility(self.actor, n.id, False, expected_revision=n2.revision)
        self.assertFalse(self.api.record_node_application(self.actor, n.id, target_revision=n2.revision, receipt='old'))
        self.assertIsNone(self.api.get_node(self.actor, n.id).applied_revision)
        self.assertTrue(self.api.record_node_application(self.actor, n.id, target_revision=n3.revision, receipt='exact'))
        self.api.observe_node(self.actor, n.id, eligible=True, source='scheduler:test', observed_at=timezone.now())
        actual = self.api.get_node(self.actor, n.id)
        self.assertFalse(actual.desired_eligible)
        self.assertTrue(actual.observed_eligible)
        self.assertEqual(actual.applied_revision, n3.revision)
        with self.assertRaisesRegex(services.UnsafeRemoval, 'deployment'):
            self.api.forget_node(self.actor, n.id, expected_revision=n3.revision)
        n4 = self.api.set_deployment_membership(self.actor, n.id, False, expected_revision=n3.revision)
        with self.assertRaisesRegex(services.UnsafeRemoval, 'fresh'):
            self.api.forget_node(self.actor, n.id, expected_revision=n4.revision)
        for change in ({'ssh_port': 0}, {'ssh_port': True}, {'driver': 'unknown'},
                       {'control_origin': 'http://remote.example'}, {'bootstrap_address': ''}):
            params = dict(name='bad', driver='host', ssh_host='ssh.example', ssh_port=22,
                control_origin='https://api.example', lan_address='10.0.0.3', bootstrap_address='boot.example')
            params.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.api.configure_node(self.actor, **params)

    def test_node_project_references_are_unique_protected_and_authorized(self):
        p, q, n = self.project(), self.project('beta'), self.node()
        restricted = services.Catalog(authorizer=Allow({p.id}))
        with self.assertRaises(PermissionError):
            restricted.set_node_projects(self.actor, n.id, (p.id, q.id), expected_revision=n.revision)
        self.assertFalse(models.NodeProject.objects.exists())
        n = self.api.set_node_projects(self.actor, n.id, (p.id,), expected_revision=n.revision)
        with self.assertRaises(IntegrityError), transaction.atomic():
            models.NodeProject.objects.create(node_id=n.id, project_id=p.id)
        with self.assertRaises(ProtectedError):
            models.Project.objects.get(pk=p.id).delete()
        with self.assertRaises(services.UnsafeRemoval):
            self.api.delete_project(self.actor, q.id, expected_revision=q.revision)

    def test_typed_settings_invalid_batch_rolls_back_and_node_scope_is_finite(self):
        initial = self.api.get_installation_settings(self.actor)
        self.assertEqual(initial.values, ())
        updated = self.api.set_installation_settings(self.actor,
            {'effect_limit': 4, 'queue_limit': 64}, expected_revision=initial.revision)
        self.assertEqual(dict(updated.values), {'effect_limit': 4, 'queue_limit': 64})
        for batch in ({'effect_limit': 5, 'queue_limit': -1}, {'effect_limit': True},
                      {'unknown': 1}, {'database_password': 'not-allowed'}, {'effect_limit': 9}):
            with self.subTest(batch=batch), self.assertRaises(ValueError):
                self.api.set_installation_settings(self.actor, batch, expected_revision=updated.revision)
            self.assertEqual(self.api.get_installation_settings(self.actor), updated)
        with self.assertRaises(services.Conflict):
            self.api.set_installation_settings(self.actor, {'effect_limit': 2}, expected_revision=initial.revision)
        n = self.node()
        ns = self.api.get_node_settings(self.actor, n.id)
        with self.assertRaises(ValueError):
            self.api.set_node_settings(self.actor, n.id, {'queue_limit': 3}, expected_revision=ns.revision)
        ns = self.api.set_node_settings(self.actor, n.id, {'node_effect_limit': 2}, expected_revision=ns.revision)
        self.assertEqual(dict(ns.values), {'node_effect_limit': 2})
        with self.assertRaises(IntegrityError), transaction.atomic():
            models.NodeSetting.objects.create(node_id=n.id, key='queue_limit', value_kind='int', int_value=3)
        with self.assertRaises(IntegrityError), transaction.atomic():
            models.InstallationSetting.objects.create(installation_id=1, key='effect_limit', value_kind='bool', bool_value=True)

    def test_settings_types_and_exact_application_receipts(self):
        initial = self.api.get_installation_settings(self.actor)
        newer = self.api.set_installation_settings(self.actor,
            {'log_level': 'info', 'telemetry_enabled': False}, expected_revision=initial.revision)
        self.assertEqual(dict(newer.values), {'log_level': 'info', 'telemetry_enabled': False})
        self.assertIsNone(newer.applied_revision)
        self.assertFalse(self.api.record_settings_application(self.actor, target_revision=initial.revision, receipt='old'))
        self.assertTrue(self.api.record_settings_application(self.actor, target_revision=newer.revision, receipt='exact'))
        self.assertEqual(self.api.get_installation_settings(self.actor).applied_revision, newer.revision)
        with self.assertRaises(ValueError):
            self.api.set_installation_settings(self.actor, {'telemetry_enabled': 1}, expected_revision=newer.revision)
        with self.assertRaises(ValueError):
            self.api.set_installation_settings(self.actor, {'log_level': 'anything'}, expected_revision=newer.revision)

    def test_settings_concurrent_batch_cas_is_aggregate_not_per_key(self):
        before = self.api.get_installation_settings(self.actor)
        def change(i):
            try:
                self.api.set_installation_settings(self.actor, {'effect_limit': i+1}, expected_revision=before.revision)
                return 'changed'
            except services.Conflict:
                return 'conflict'
        self.assertCountEqual(self.race(change), ['changed', 'conflict'])

    def pin(self, p, payload=b'role: worker\n# exact bytes\n'):
        return self.api.pin_project_policy(self.actor, p.id, payload, source_commit='a'*40,
            source_ref='refs/heads/main', provenance='git:test', cpu=100, memory_mb=256,
            disk_mb=1024, expected_revision=p.revision)

    def test_policy_snapshot_exact_immutable_bytes_digest_and_protected_evidence(self):
        p = self.project()
        payload = b'# exact\r\nrole: worker\n'
        snap = self.pin(p, payload)
        self.assertEqual(snap.content, payload)
        self.assertEqual(snap.digest, hashlib.sha256(payload).hexdigest())
        self.assertEqual((snap.source_commit, snap.source_ref, snap.provenance), ('a'*40, 'refs/heads/main', 'git:test'))
        self.assertEqual(self.api.get_policy(self.actor, snap.id), snap)
        row = models.ProjectPolicySnapshot.objects.get(pk=snap.id)
        row.content = b'tampered'
        with self.assertRaisesRegex(ValueError, 'immutable'):
            row.save()
        with self.assertRaisesRegex(ValueError, 'immutable'):
            models.ProjectPolicySnapshot.objects.filter(pk=snap.id).update(content=b'tampered')
        with self.assertRaises(ProtectedError):
            models.Project.objects.get(pk=p.id).delete()
        with self.assertRaises(services.ProtectionUnavailable):
            self.api.pin_project_policy(self.actor, p.id, b'confidential', source_commit='b'*40,
                source_ref='main', provenance='test', cpu=100, memory_mb=256, disk_mb=1024,
                confidential=True, expected_revision=2)
        self.assertEqual(models.ProjectPolicySnapshot.objects.count(), 1)
        for resource in (0, -1, True):
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                self.api.pin_project_policy(self.actor, p.id, b'next', source_commit='c'*40,
                    source_ref='main', provenance='test', cpu=resource, memory_mb=256, disk_mb=1024,
                    expected_revision=2)

    def test_image_intent_is_not_availability_and_cross_project_ref_is_refused(self):
        p, q, n = self.project(), self.project('beta'), self.node()
        policy, other = self.pin(p), self.pin(q)
        image = self.api.create_image_revision(self.actor, p.id, policy.id, source='builder:input')
        foreign = self.api.create_image_revision(self.actor, q.id, other.id, source='builder:other')
        with self.assertRaises(ValueError):
            self.api.create_image_revision(self.actor, p.id, other.id, source='forged')
        state = self.api.set_image_intent(self.actor, n.id, p.id, image.id, expected_revision=0)
        self.assertIsNone(state.observed_image_id)
        self.assertIsNone(state.available)
        with self.assertRaises(ValueError):
            self.api.set_image_intent(self.actor, n.id, p.id, foreign.id, expected_revision=state.revision)
        state = self.api.observe_image(self.actor, n.id, p.id, image.id, available=True,
            source='scheduler:meta', observed_at=timezone.now(), receipt='external-1', expected_revision=state.revision)
        self.assertTrue(state.available)
        self.assertEqual(state.source, 'scheduler:meta')
        with self.assertRaises(ValueError):
            self.api.observe_image(self.actor, n.id, p.id, foreign.id, available=True,
                source='scheduler:meta', observed_at=timezone.now(), receipt='forged', expected_revision=state.revision)

    def test_scope_checks_existing_identity_and_old_and_new_references(self):
        p, q, n = self.project(), self.project('beta'), self.node()
        denied = services.Catalog(authorizer=Allow(set()))
        with self.assertRaises(PermissionError):
            denied.register_project(self.actor, p.origin)
        n = self.api.set_node_projects(self.actor, n.id, (p.id, q.id), expected_revision=n.revision)
        with self.assertRaises(PermissionError):
            services.Catalog(authorizer=Allow({p.id})).set_node_projects(
                self.actor, n.id, (p.id,), expected_revision=n.revision)
        self.assertEqual(models.NodeProject.objects.filter(node_id=n.id).count(), 2)
        snap = self.pin(q)
        with self.assertRaises(PermissionError):
            services.Catalog(authorizer=Allow({p.id})).get_policy(self.actor, snap.id)

    def test_client_binding_is_durable_metadata_not_presence(self):
        p = self.project()
        client = self.api.register_client(self.actor, label='workstation')
        self.assertFalse(hasattr(client, 'online'))
        binding = self.api.bind_workspace(self.actor, client.id, 'checkout-one', p.id,
            integration_branch='epic/test', expected_revision=0)
        self.assertEqual(self.api.resolve_workspace(self.actor, client.id, 'checkout-one'), binding)
        self.assertEqual(binding.project_id, p.id)
        with self.assertRaises(ProtectedError):
            models.ClientRegistration.objects.get(pk=client.id).delete()
        with self.assertRaises(PermissionError):
            services.Catalog(authorizer=Allow(set())).resolve_workspace(self.actor, client.id, 'checkout-one')

    def test_default_deny_scope_refs_and_database_failure_never_defaults(self):
        with self.assertRaises(PermissionError):
            services.Catalog().register_project(self.actor, 'https://git.example/team/nope.git')
        p = self.project()
        with self.assertRaises(PermissionError):
            services.Catalog(authorizer=Allow(set())).get_project(self.actor, p.id)
        self.assertEqual(self.auth.calls[-1][2], (p.id,))
        self.assertIsInstance(self.api.list_projects(self.actor), tuple)
        from django.db import connection
        connection.ensure_connection()
        connection.connection.pgconn.finish()
        try:
            with self.assertRaises(DatabaseError):
                self.api.list_projects(self.actor)
            with self.assertRaises(DatabaseError):
                self.api.get_installation_settings(self.actor)
        finally:
            connections.close_all()

    def test_protection_port_rechecks_authority_after_wait(self):
        from catalog_schema import protection_reauthorizes_after_external_wait
        protection_reauthorizes_after_external_wait()

    def test_catalogue_has_no_old_store_or_late_domain_imports(self):
        import ast
        from pathlib import Path
        root = Path(services.__file__).parent
        forbidden = ('mop.common.config', 'mop.client', 'mop.server.access',
                     'mop.server.operations', 'mop.server.secrets', 'mop.server.nodes')
        for path in root.rglob('*.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or '').startswith(forbidden), str(path))
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, ('getenv', 'environ'), str(path))
