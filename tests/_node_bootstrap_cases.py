"""Реальный PostgreSQL/Actor/Token/HTTP контракт private freshNode export #722."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from uuid import uuid4
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission, Group
from django.test import TransactionTestCase, Client, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from mop.server.access import models as a, services as auth
from mop.server.catalog.models import Node, Project, NodeProject
from mop.server.controller import runtime
from mop.server.controller.models import Installation, RestoreGuard
from mop.server.sessions.models import NodeExecutor

# HYPOTHESIS: нет поддержанного issuer, empty executor не узнаёт previous epoch.
# SPEC: standard service Actor/enrollment/token, fixed canonical permissions,
# exact current NodeProjects; reexport только по exact live enrollment ID;
# revoke/shape drift не ремонтируются, экспорт никогда не включает authority.
# SOLUTION: standard enroll_service в maintenance transaction, exact live
# reexport без renew; initial NodeExecutor epoch и private transport export.
# RESULT: исходный RED — 11 missing-issuer assertions на PostgreSQL 17, не setup.
# Дополнительные RED: raw DB error, отсутствующий reexport handle, scope race и
# parent fsync после commit. Все 14 checks зелёные на реальном PostgreSQL 17.
# Physical poll/facts ports ограничены test adapters; live node не проверялся.
# STATUS: FIXED — see #722
ACTIONS = frozenset('execution.' + name for name in (
    'poll_work', 'claim_work', 'report_result', 'read_facts', 'read_launch',
    'register_producer', 'ingest_telemetry', 'ingest_observations'))


@override_settings(ALLOWED_HOSTS=['testserver'])
class NodeBootstrapExport(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        runtime.initialize()
        self.assertTrue(callable(getattr(runtime, 'enroll_node', None)),
                        'supported maintenance node enrollment/export is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.node = self.node_record('one')
        self.other = self.node_record('two')
        self.project = Project.objects.create(name='one', origin='https://example.invalid/one.git')
        self.foreign = Project.objects.create(name='foreign', origin='https://example.invalid/foreign.git')
        NodeProject.objects.create(node=self.node, project=self.project)

    def node_record(self, name):
        return Node.objects.create(name=name, driver='host', ssh_host='example.invalid',
            control_origin='https://example.invalid', lan_address='example.invalid',
            bootstrap_address='example.invalid')

    def export(self, folder='new', **changes):
        return runtime.enroll_node(**{**dict(node_id=self.node.pk,
            origin='https://controller.example.invalid', ca_file='/etc/mop/ca.pem',
            file=self.root/folder/'bootstrap.json', node_file='/var/lib/mop-node/bootstrap.json'), **changes})

    def issued(self):
        value = self.export()
        data = json.loads((self.root/'new'/'bootstrap.json').read_bytes())
        enrollment = a.ServiceEnrollment.objects.get(pk=value['enrollment_id'])
        return value, data, enrollment, enrollment.actor

    def request(self, data, path, params=None):
        return Client().get('/api/v1/'+path, params or {}, secure=True,
                            HTTP_AUTHORIZATION='Token '+data['token'])

    def assert_private_unchanged(self, data, user, enrollment):
        self.assertEqual(Token.objects.get(user=user).key, data['token'])
        self.assertEqual(a.ServiceEnrollment.objects.get(actor=user).pk, enrollment.pk)
        self.assertEqual(get_user_model().objects.filter(kind='service').count(), 1)

    def test_issue_is_standard_actor_fixed_permissions_exact_current_projects_and_minimal_file(self):
        retired_id = uuid4()
        retired = Project.objects.create(id=retired_id, name='old', origin='https://example.invalid/old.git',
                                         retired_at=timezone.now(), identity_slot=retired_id)
        NodeProject.objects.create(node=self.node, project=retired)
        value, data, enrollment, user = self.issued()
        self.assertEqual(set(data), {'schema_version', 'installation_id', 'origin', 'ca_file', 'node_id', 'token'})
        self.assertEqual(data['schema_version'], 1)
        self.assertEqual(data['node_id'], str(self.node.pk))
        self.assertEqual(data['installation_id'], 'isolated-ci')
        self.assertNotIn(data['token'], repr(value))
        delivery = json.loads((self.root/'new'/'delivery.json').read_bytes())
        self.assertEqual(delivery.get('enrollment_id'), str(enrollment.pk),
                         'quiet CLI must leave a nonsecret exact reexport handle in the delivery receipt')
        self.assertNotIn(data['token'], repr(delivery))
        self.assertEqual(user.kind, 'service')
        self.assertFalse(user.has_usable_password())
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_superuser or user.is_staff)
        self.assertFalse(user.groups.exists())
        self.assertFalse(a.LoginIdentity.objects.filter(user=user).exists())
        self.assertFalse(a.AllProjectMembership.objects.filter(user=user).exists())
        self.assertEqual(set(user.get_all_permissions()), ACTIONS)
        self.assertEqual(set(a.ProjectMembership.objects.filter(user=user).values_list('project_id', flat=True)),
                         {self.project.pk})
        self.assertEqual(enrollment.use_case, 'node-executor')
        self.assertEqual(enrollment.node_id, self.node.pk)
        actor = auth.token_context(data['token'])
        self.assertEqual(actor.user_id, user.pk)
        self.assertEqual(actor.node_id, self.node.pk)
        self.assertIsNone(actor.human_owner_id)
        for action in ('controller.manage_proxy', 'catalog.configure_node', 'access.activate_grant',
                       'secrets.deliver_secret', 'operations.stop_puppet_job'):
            with self.assertRaises(auth.Denied):
                auth.Access().authorize(actor, action, node_ids=[self.node.pk])
        audit = list(a.CredentialEvent.objects.values())
        self.assertNotIn(data['token'], repr(audit))

    def test_empty_node_discovers_initial_epoch_without_enabling_effects_or_physical_capabilities(self):
        _, data, _, _ = self.issued()
        initial = NodeExecutor.objects.get(node=self.node).executor_epoch
        self.assertFalse(Installation.objects.get(pk=1).effects_enabled)
        response = self.request(data, 'machine/executor', {'schema_version':'1', 'node_id':str(self.node.pk)})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['executor_epoch'], str(initial))
        identity = self.request(data, 'identity')
        self.assertEqual(identity.status_code, 200, identity.content)
        self.assertEqual(identity.json()['kind'], 'service')
        self.assertIsNone(identity.json()['human_owner_id'])
        # Work adapter по умолчанию требует ещё не связанный physical port.
        # Здесь только пустая addressed queue: реальный HTTP/auth/scope pipeline,
        # но не заявление, что private export связывает production executor.
        from mop.server.controller import machineviews
        with patch.object(machineviews.execution.work, 'pending', return_value=()):
            poll = self.request(data, 'machine/work', {'schema_version':'1', 'node_id':str(self.node.pk),
                                                     'executor_epoch':str(initial)})
        self.assertEqual(poll.status_code, 200, poll.content)
        self.assertEqual(poll.json()['work'], [])
        # Незавязанный physical reader всё ещё unavailable, не пустой success.
        facts = self.request(data, 'machine/facts', {'schema_version':'1', 'node_id':str(self.node.pk), 'kind':'health'})
        self.assertEqual(facts.status_code, 503)
        self.assertFalse(Installation.objects.get(pk=1).effects_enabled)

    def test_facts_and_every_fixed_permission_use_current_node_and_project_admission(self):
        _, data, _, _ = self.issued()
        actor = auth.token_context(data['token'])
        for action in ACTIONS:
            auth.Access().authorize(actor, action, node_ids=[self.node.pk], project_ids=[self.project.pk])
            with self.assertRaises(auth.Denied):
                auth.Access().authorize(actor, action, node_ids=[self.other.pk], project_ids=[self.project.pk])
            with self.assertRaises(auth.Denied):
                auth.Access().authorize(actor, action, node_ids=[self.node.pk], project_ids=[self.foreign.pk])
        from mop.server.sessions.protocol import FactPageV1
        from mop.server.controller.machinebinding import authority
        class Reader:
            def read(inner, *, node_id, kind, limit):
                return FactPageV1(node_id, kind, True, ())
        service = runtime.machine_execution_service(facts=Reader(), installation=authority)
        self.assertTrue(service.facts(actor, self.node.pk, kind='health').complete)
        with self.assertRaises(auth.Denied):
            service.facts(actor, self.other.pk, kind='health')
        NodeProject.objects.create(node=self.node, project=self.foreign)
        with self.assertRaises(auth.Denied):
            auth.Access().authorize(actor, 'execution.read_facts', node_ids=[self.node.pk], project_ids=[self.foreign.pk])

    def test_reexport_requires_exact_live_enrollment_and_never_rotates_or_resets_runtime_epoch(self):
        value, data, enrollment, user = self.issued()
        epoch = uuid4()
        NodeExecutor.objects.filter(node=self.node).update(executor_epoch=epoch)
        events = a.CredentialEvent.objects.count()
        for folder, changes in (('implicit', {}), ('wrong-id', {'enrollment_id':uuid4()}),
                                ('wrong-node', {'node_id':self.other.pk, 'enrollment_id':enrollment.pk})):
            with self.assertRaises(RuntimeError):
                self.export(folder, **changes)
            self.assertFalse((self.root/folder).exists())
        again = self.export('again', enrollment_id=enrollment.pk)
        self.assertEqual(again['enrollment_id'], value['enrollment_id'])
        self.assertEqual(json.loads((self.root/'again'/'bootstrap.json').read_bytes()), data)
        self.assert_private_unchanged(data, user, enrollment)
        self.assertEqual(a.CredentialEvent.objects.count(), events)
        self.assertEqual(NodeExecutor.objects.get(node=self.node).executor_epoch, epoch)

    def test_revoked_token_or_removed_enrollment_cannot_be_reissued_by_export(self):
        _, data, enrollment, user = self.issued()
        actor = auth.token_context(data['token'])
        auth.revoke_token(actor)
        self.assertIn(self.request(data, 'identity').status_code, (401,403))
        with self.assertRaises(RuntimeError):
            self.export('revoked', enrollment_id=enrollment.pk)
        self.assertFalse(Token.objects.filter(user=user).exists())
        self.assertFalse((self.root/'revoked').exists())
        a.ServiceEnrollment.objects.filter(pk=enrollment.pk).delete()
        with self.assertRaises(RuntimeError):
            self.export('removed', enrollment_id=enrollment.pk)
        self.assertEqual(get_user_model().objects.filter(kind='service').count(), 1)

    def test_permission_membership_purpose_and_actor_drift_refuse_without_repair(self):
        _, data, enrollment, user = self.issued()
        permission = Permission.objects.get(content_type__app_label='execution', codename='read_facts')
        user.user_permissions.remove(permission)
        with self.assertRaises(RuntimeError):
            self.export('missing-permission', enrollment_id=enrollment.pk)
        self.assertFalse(user.user_permissions.filter(pk=permission.pk).exists())
        user.user_permissions.add(permission)
        NodeProject.objects.create(node=self.node, project=self.foreign)
        with self.assertRaises(RuntimeError):
            self.export('project-expansion', enrollment_id=enrollment.pk)
        self.assertFalse(a.ProjectMembership.objects.filter(user=user, project=self.foreign).exists())
        NodeProject.objects.filter(node=self.node, project=self.foreign).delete()
        group = Group.objects.create(name='unexpected-group')
        user.groups.add(group)
        with self.assertRaises(RuntimeError):
            self.export('group', enrollment_id=enrollment.pk)
        user.groups.clear()
        a.ServiceEnrollment.objects.filter(pk=enrollment.pk).update(use_case='grant-activator')
        with self.assertRaises(RuntimeError):
            self.export('purpose', enrollment_id=enrollment.pk)
        a.ServiceEnrollment.objects.filter(pk=enrollment.pk).update(use_case='node-executor')
        get_user_model().objects.filter(pk=user.pk).update(kind='human')
        with self.assertRaises(RuntimeError):
            self.export('human', enrollment_id=enrollment.pk)
        self.assertEqual(get_user_model().objects.get(pk=user.pk).kind, 'human')
        self.assertEqual(Token.objects.get(user=user).key, data['token'])

    def test_username_collision_is_not_ownership_and_no_actor_is_modified(self):
        username = 'node-executor-' + self.node.pk.hex
        human = get_user_model().objects.create_user(username, password='test-only-human-password')
        with self.assertRaises(RuntimeError):
            self.export()
        human.refresh_from_db()
        self.assertEqual(human.kind, 'human')
        self.assertTrue(human.check_password('test-only-human-password'))
        self.assertEqual(a.ServiceEnrollment.objects.count(), 0)
        self.assertFalse((self.root/'new').exists())
        self.assertFalse(NodeExecutor.objects.filter(node=self.node).exists())

    def test_existing_output_refused_before_db_mutation(self):
        folder = self.root/'new'
        folder.mkdir(mode=0o700)
        (folder/'bootstrap.json').write_text('untouched')
        with self.assertRaises(RuntimeError):
            self.export()
        self.assertEqual((folder/'bootstrap.json').read_text(), 'untouched')
        self.assertEqual(a.ServiceEnrollment.objects.count(), 0)
        self.assertEqual(NodeExecutor.objects.count(), 0)

    def test_wrong_role_installation_restore_guard_and_retired_node_fail_before_export(self):
        from django.conf import settings
        for role in ('web', 'worker', 'credential', 'gateway'):
            with override_settings(BOOTSTRAP={**settings.BOOTSTRAP, 'role':role}), self.assertRaises(RuntimeError):
                self.export()
        with override_settings(BOOTSTRAP={**settings.BOOTSTRAP, 'installation_id':'wrong'}), self.assertRaises(RuntimeError):
            self.export()
        RestoreGuard.objects.filter(pk=1).update(state='restoring')
        with self.assertRaises(RuntimeError):
            self.export()
        RestoreGuard.objects.filter(pk=1).update(state='ready')
        Node.objects.filter(pk=self.node.pk).update(retired_at=timezone.now(), identity_slot=self.node.pk)
        with self.assertRaises(RuntimeError):
            self.export()
        self.assertFalse((self.root/'new').exists())
        self.assertEqual(a.ServiceEnrollment.objects.count(), 0)

    def test_write_failure_rolls_back_credentials_epoch_and_new_files_with_safe_error(self):
        with patch('mop.server.controller.nodebootstrap.ExportFiles.write', side_effect=OSError('private-token-value')):
            with self.assertRaises(RuntimeError) as caught:
                self.export()
        self.assertNotIn('private-token-value', str(caught.exception))
        self.assertEqual(a.ServiceEnrollment.objects.count(), 0)
        self.assertEqual(Token.objects.count(), 0)
        self.assertEqual(NodeExecutor.objects.count(), 0)
        self.assertFalse((self.root/'new').exists())

    def test_directory_durability_failure_cannot_commit_an_unexported_enrollment(self):
        import os
        original = os.fsync
        parent_inode = self.root.stat().st_ino
        def fail_parent(descriptor):
            if os.fstat(descriptor).st_ino == parent_inode:
                raise OSError('private-token-value')
            return original(descriptor)
        with patch('os.fsync', side_effect=fail_parent):
            with self.assertRaises(RuntimeError) as caught:
                self.export()
        self.assertNotIn('private-token-value', str(caught.exception))
        self.assertEqual(a.ServiceEnrollment.objects.count(), 0)
        self.assertEqual(Token.objects.count(), 0)
        self.assertEqual(NodeExecutor.objects.count(), 0)
        self.assertFalse((self.root/'new').exists())

    def test_project_scope_change_before_node_lock_cannot_export_a_stale_subset(self):
        original = Node.objects.select_for_update
        injected = False
        def change_scope(*args, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                NodeProject.objects.create(node=self.node, project=self.foreign)
            return original(*args, **kwargs)
        with patch.object(Node.objects, 'select_for_update', side_effect=change_scope):
            with self.assertRaisesRegex(RuntimeError, 'project|scope'):
                self.export()
        self.assertEqual(a.ServiceEnrollment.objects.count(), 0)
        self.assertFalse((self.root/'new').exists())

    def test_early_database_errors_are_sanitized_before_export(self):
        from django.db import DatabaseError
        with patch.object(runtime, 'verify_installation', side_effect=DatabaseError('private-token-value')):
            with self.assertRaises(RuntimeError) as caught:
                self.export()
        self.assertNotIn('private-token-value', str(caught.exception))
        self.assertIn('DatabaseError', str(caught.exception))
        self.assertFalse((self.root/'new').exists())

    def test_issuer_and_cli_never_print_raw_credentials(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            _, data, _, _ = self.issued()
        self.assertNotIn(data['token'], output.getvalue()+errors.getvalue())
