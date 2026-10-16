"""Настоящие Admin requests: adapters не становятся вторым владельцем состояния."""
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from mop.server.access import models as a, services as access
from mop.server.catalog import models as c
from mop.server.controller.models import Installation, RestoreGuard
from mop.server.controller.runtime import initialize
from mop.server.operations import models as o
from mop.server.secrets import models as s


# FEATURE: #638 — Admin format/UX + dispatch к общим use cases.
# HYPOTHESIS: свежая ORM revision в save_model теряет CAS формы; generic
# Admin hooks допускают обход domain scope и раскрывают защищённые поля.
# SPEC: реальные POST с устаревшим revision, downgrade, FK и bulk обходами;
# штатный PostgreSQL, без browser/pool/root inputs.
# SOLUTION: form revision передаётся Catalog; physical admission отдельно от
# changeform transaction; защищённая замена идёт через настоящий #669 socket.
# RED: PG16 — stale POST вернул 302 вместо 409, новые маршруты отсутствовали;
# PG17 — отдельный regression red для ссылки на независимый gateway.
# RED: replay после downgrade, recent LogEntry/flash, forged save-as-new,
# duplicate add и stale CAS при совпавших значениях воспроизведены POST-ами.
# RESULT: PG16/17 — 34/34 (31 Admin + 3 Auth), включая настоящий worker с
# контролируемым adapter и защищённый socket без web root provider.
# Полный strict/native-TLS gate остаётся CI на точном branch SHA.
# STATUS: FIXED — see #638
@override_settings(ALLOWED_HOSTS=['testserver'])
class OperationBackedAdmin(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        RestoreGuard.objects.get_or_create(pk=1)
        initialize()
        self.installation = Installation.objects.get(pk=1)
        self.source = a.IdentitySource.objects.create(name='local', kind='local')
        self.user = get_user_model().objects.create_user('operator', password='test-only-password', is_staff=True)
        a.Principal.objects.create(user=self.user, kind='human')
        a.LoginIdentity.objects.create(user=self.user, source=self.source, external_subject='operator')
        self.project = c.Project.objects.create(name='owned', origin='https://example.invalid/owned.git')
        self.foreign = c.Project.objects.create(name='foreign', origin='https://example.invalid/foreign.git')
        a.ProjectMembership.objects.create(user=self.user, project=self.project, source=self.source)
        self.client = Client()
        response = self.client.post('/api/v1/auth/login',
            {'username': 'operator', 'password': 'test-only-password'}, secure=True)
        self.assertEqual(response.status_code, 200)
        self.allow('catalog.view_project')

    def allow(self, *names):
        for name in names:
            app, code = name.split('.')
            if app == 'controller':
                permission = Permission.objects.get_by_natural_key(code, app, 'domainpermission')
            elif app == 'auth':  # logical namespace; rows live under actor label
                code = code.replace('_user', '_actor', 1)
                permission = Permission.objects.get(content_type__app_label='actor', codename=code)
            else:
                permission = Permission.objects.get(content_type__app_label=app, codename=code)
            self.user.user_permissions.add(permission)

    def global_scope(self):
        a.AllProjectMembership.objects.get_or_create(user=self.user, source=self.source)

    def post(self, url, data):
        return self.client.post(url, data, secure=True)

    def get(self, url):
        return self.client.get(url, secure=True)

    def project_form(self, **changes):
        values = {'origin': self.project.origin, 'puppet_limit': '4', 'expected_revision': '1', '_save': 'Save'}
        values.update(changes)
        return values

    def node(self):
        return c.Node.objects.create(name='node', driver='host', ssh_host='node.invalid',
            control_origin='https://node.invalid', lan_address='node.invalid', bootstrap_address='node.invalid')

    def test_stale_project_revision_is_html_conflict_not_last_write_wins(self):
        self.global_scope()
        self.allow('catalog.change_project', 'catalog.configure_project', 'catalog.set_project_limit')
        url = f'/admin/catalog/project/{self.project.pk}/change/'
        self.assertEqual(self.get(url).status_code, 200)
        c.Project.objects.filter(pk=self.project.pk).update(revision=2, puppet_limit=7)
        response = self.post(url, self.project_form())
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response['Content-Type'].startswith('text/html'))
        self.project.refresh_from_db()
        self.assertEqual((self.project.revision, self.project.puppet_limit), (2, 7))

    def test_project_add_and_limit_form_use_catalogue_commands(self):
        self.global_scope()
        self.allow('catalog.add_project', 'catalog.change_project', 'catalog.configure_project', 'catalog.set_project_limit')
        response = self.post('/admin/catalog/project/add/', {'origin': 'https://example.invalid/new.git',
            'puppet_limit': '0', '_save': 'Save'})
        self.assertEqual(response.status_code, 302)
        row = c.Project.objects.get(name='new')
        self.assertEqual((row.puppet_limit, row.revision), (0, 2))
        response = self.post(f'/admin/catalog/project/{row.pk}/change/',
            {'origin': row.origin, 'puppet_limit': '3', 'expected_revision': '2', '_save': 'Save'})
        self.assertEqual(response.status_code, 302)
        row.refresh_from_db()
        self.assertEqual((row.puppet_limit, row.revision), (3, 3))

    def test_project_limit_form_can_restore_the_supported_unlimited_value(self):
        self.global_scope()
        self.allow('catalog.change_project', 'catalog.configure_project', 'catalog.set_project_limit')
        c.Project.objects.filter(pk=self.project.pk).update(puppet_limit=4)
        response = self.post(f'/admin/catalog/project/{self.project.pk}/change/',
                             self.project_form(puppet_limit=''))
        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual((self.project.puppet_limit, self.project.revision), (None, 2))

    def test_forged_save_as_new_cannot_turn_stale_change_into_fresh_add(self):
        self.global_scope()
        self.allow('catalog.add_project', 'catalog.change_project', 'catalog.configure_project', 'catalog.set_project_limit')
        c.Project.objects.filter(pk=self.project.pk).update(revision=2, puppet_limit=7)
        response = self.post(f'/admin/catalog/project/{self.project.pk}/change/',
            {**self.project_form(), '_saveasnew': 'Save as new'})
        self.assertEqual(response.status_code, 403)
        self.project.refresh_from_db()
        self.assertEqual((self.project.revision, self.project.puppet_limit), (2, 7))

    def test_duplicate_add_is_not_a_revision_free_update_of_an_existing_project(self):
        self.global_scope()
        self.allow('catalog.add_project', 'catalog.change_project', 'catalog.configure_project', 'catalog.set_project_limit')
        c.Project.objects.filter(pk=self.project.pk).update(revision=2, puppet_limit=7)
        response = self.post('/admin/catalog/project/add/', {'origin': self.project.origin,
            'puppet_limit': '4', '_save': 'Save'})
        self.assertEqual(response.status_code, 409)
        self.project.refresh_from_db()
        self.assertEqual((self.project.revision, self.project.puppet_limit), (2, 7))

    def test_matching_fresh_values_do_not_erase_the_submitted_stale_revision(self):
        self.global_scope()
        self.allow('catalog.change_project', 'catalog.configure_project', 'catalog.set_project_limit')
        c.Project.objects.filter(pk=self.project.pk).update(revision=2, puppet_limit=4)
        response = self.post(f'/admin/catalog/project/{self.project.pk}/change/', self.project_form())
        self.assertEqual(response.status_code, 409)
        self.assertFalse(LogEntry.objects.filter(object_id=str(self.project.pk)).exists())

    def test_staff_without_domain_permission_and_post_downgrade_are_denied(self):
        self.global_scope()
        self.allow('catalog.change_project', 'catalog.configure_project', 'catalog.set_project_limit')
        url = f'/admin/catalog/project/{self.project.pk}/change/'
        self.assertEqual(self.get(url).status_code, 200)
        self.user.user_permissions.clear()
        self.assertEqual(self.post(url, self.project_form()).status_code, 403)
        self.assertEqual(self.get('/admin/catalog/project/').status_code, 403)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.puppet_limit)

    def test_foreign_project_list_count_detail_and_inline_do_not_leak(self):
        response = self.get('/admin/catalog/project/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['cl'].result_count, 1)
        self.assertNotContains(response, self.foreign.origin)
        response = self.get(f'/admin/catalog/project/{self.foreign.pk}/change/')
        self.assertIn(response.status_code, (302, 404))
        response = self.post(f'/admin/catalog/project/{self.project.pk}/change/', {
            **self.project_form(), 'nodeproject_set-TOTAL_FORMS': '1',
            'nodeproject_set-INITIAL_FORMS': '0', 'nodeproject_set-0-project': self.foreign.pk})
        self.assertIn(response.status_code, (400, 403))
        self.assertFalse(c.NodeProject.objects.exists())

    def test_delete_selected_and_direct_delete_never_delete_catalogue(self):
        self.global_scope()
        self.allow('catalog.delete_project', 'catalog.configure_project')
        for data in ({'action': 'delete_selected', '_selected_action': [str(self.project.pk)], 'post': 'yes'},
                     {'action': 'delete_selected', 'select_across': '1', 'post': 'yes'}):
            response = self.post('/admin/catalog/project/', data)
            self.assertIn(response.status_code, (400, 403))
        self.assertEqual(self.post(f'/admin/catalog/project/{self.project.pk}/delete/', {'post': 'yes'}).status_code, 403)
        self.assertEqual(c.Project.objects.count(), 2)

    def test_node_form_filters_project_choices_and_revalidates_fks(self):
        self.global_scope()
        self.allow('catalog.add_node', 'catalog.view_node', 'catalog.configure_node', 'catalog.change_node')
        response = self.get('/admin/catalog/node/add/')
        self.assertEqual(response.status_code, 200)
        # GET был глобальным; к POST scope уже уже, включая FK-подмену.
        a.AllProjectMembership.objects.filter(user=self.user).delete()
        response = self.post('/admin/catalog/node/add/', {'name': 'forged', 'driver': 'host',
            'ssh_host': 'node.invalid', 'ssh_port': '22', 'control_origin': 'https://node.invalid',
            'lan_address': 'node.invalid', 'bootstrap_address': 'node.invalid',
            'projects': [str(self.foreign.pk)], '_save': 'Save'})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(c.Node.objects.exists())

    def test_node_catalogue_save_does_not_apply_observations_or_eligibility(self):
        self.global_scope()
        self.allow('catalog.add_node', 'catalog.view_node', 'catalog.change_node', 'catalog.configure_node')
        response = self.post('/admin/catalog/node/add/', {'name': 'new-node', 'driver': 'host',
            'ssh_host': 'node.invalid', 'ssh_port': '22', 'control_origin': 'https://node.invalid',
            'lan_address': 'node.invalid', 'bootstrap_address': 'node.invalid',
            'projects': [str(self.project.pk)], 'deployment_member': 'on',
            'observed_eligible': 'on', 'desired_eligible': '', '_save': 'Save'})
        self.assertEqual(response.status_code, 302)
        row = c.Node.objects.get()
        self.assertIsNone(row.observed_eligible)
        self.assertTrue(row.desired_eligible)
        self.assertTrue(row.deployment_member)
        self.assertEqual(tuple(row.nodeproject_set.values_list('project_id', flat=True)), (self.project.pk,))
        self.assertEqual(o.Operation.objects.count(), 0)

    def test_node_edit_uses_the_submitted_catalogue_revision(self):
        self.global_scope()
        self.allow('catalog.view_node', 'catalog.change_node', 'catalog.configure_node')
        node = self.node()
        url = f'/admin/catalog/node/{node.pk}/change/'
        self.assertEqual(self.get(url).status_code, 200)
        c.Node.objects.filter(pk=node.pk).update(revision=2)
        response = self.post(url, {'name': node.name, 'driver': 'host', 'ssh_host': 'changed.invalid',
            'ssh_port': '22', 'control_origin': node.control_origin, 'lan_address': node.lan_address,
            'bootstrap_address': node.bootstrap_address, 'expected_revision': '1', '_save': 'Save'})
        self.assertEqual(response.status_code, 409)
        node.refresh_from_db()
        self.assertEqual((node.ssh_host, node.revision), ('node.invalid', 2))

    def test_node_settings_use_settings_revision_and_do_not_apply_node_state(self):
        self.global_scope()
        self.allow('catalog.view_node', 'catalog.view_nodesetting', 'catalog.change_nodesetting', 'catalog.configure_node')
        node = self.node()
        c.Node.objects.filter(pk=node.pk).update(revision=8)
        url = '/admin/catalog/nodesetting/configure/?node=' + str(node.pk)
        response = self.post(url, {'node': str(node.pk), 'expected_revision': '1', 'log_level': 'warning'})
        self.assertEqual(response.status_code, 302)
        node.refresh_from_db()
        self.assertEqual((node.revision, node.settings_revision, node.settings_applied_revision), (8, 2, None))
        self.assertEqual(c.NodeSetting.objects.get(node=node, key='log_level').text_value, 'warning')
        self.assertEqual(self.post(url, {'node': str(node.pk), 'expected_revision': '1', 'log_level': 'error'}).status_code, 409)
        self.assertFalse(o.Operation.objects.exists())

    def test_typed_settings_save_is_desired_revision_only_and_stale_is_conflict(self):
        self.global_scope()
        self.allow('catalog.view_installationsetting', 'catalog.change_installationsetting')
        url = '/admin/catalog/installationsetting/configure/'
        response = self.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'text_value')
        self.assertEqual(self.post(url, {'effect_limit': '3', 'expected_revision': '1'}).status_code, 302)
        state = c.InstallationConfiguration.objects.get()
        self.assertEqual((state.revision, state.applied_revision), (2, None))
        self.assertEqual(c.InstallationSetting.objects.get(key='effect_limit').int_value, 3)
        self.assertEqual(self.post(url, {'effect_limit': '4', 'expected_revision': '1'}).status_code, 409)
        self.assertEqual(o.Operation.objects.count(), 0)

    def test_settings_reject_unknown_names_and_wrong_types(self):
        self.global_scope()
        self.allow('catalog.view_installationsetting', 'catalog.change_installationsetting')
        url = '/admin/catalog/installationsetting/configure/'
        for fields in ({'not_a_setting': 'secret'}, {'effect_limit': 'false'}, {'effect_limit': '100'}):
            response = self.post(url, {'expected_revision': '1', **fields})
            self.assertEqual(response.status_code, 400)
        self.assertFalse(c.InstallationSetting.objects.exists())

    def test_operation_admin_is_readonly_and_does_not_expose_actor_binding(self):
        self.assertTrue(admin.site.is_registered(o.Operation), 'Operation must have a readonly progress page')
        self.assertFalse(admin.site._registry[o.Operation].has_add_permission(self.get('/admin/').wsgi_request))
        self.global_scope()
        self.allow('operations.view_operation', 'catalog.drain_node')
        row = self.admit_node()
        response = self.get(f"/admin/operations/operation/{row['id']}/change/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'actor_binding')
        self.assertContains(response, '/api/v1/operations/' + row['id'])
        response = self.post(f"/admin/operations/operation/{row['id']}/change/", {'state': 'succeeded', '_save': 'Save'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(o.Operation.objects.get().state, 'accepted')

    def admit_node(self):
        from mop.server.operations.node import NodeEligibility
        from mop.server.controller.runtime import operations_service
        Installation.objects.update(effects_enabled=True)
        node = self.node()
        c.NodeProject.objects.create(node=node, project=self.project)
        self.action_node = node
        command = NodeEligibility(self.installation.epoch, uuid4(), node.pk, 1,
            timezone.now() + timedelta(minutes=2), False)
        return operations_service().submit(access.session_context(self.client.session.session_key), command)

    def test_unbound_physical_handler_is_visible_unavailable_and_cannot_accept(self):
        self.global_scope()
        self.allow('catalog.view_node', 'catalog.drain_node', 'operations.view_operation')
        node = self.node()
        url = f'/admin/catalog/node/{node.pk}/actions/drain/'
        response = self.get(url)
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, 'unavailable', status_code=503)
        response = self.post(url, {'expected_revision': '1', 'request_id': str(uuid4()),
            'epoch': str(self.installation.epoch), 'not_after': (timezone.now()+timedelta(minutes=2)).isoformat()})
        self.assertEqual(response.status_code, 503)
        self.assertFalse(o.Operation.objects.exists())

    def test_registered_node_action_uses_actual_operation_admission_and_replay(self):
        self.global_scope()
        self.allow('catalog.view_node', 'catalog.drain_node', 'catalog.enable_node', 'operations.view_operation')
        node = self.node()
        Installation.objects.update(effects_enabled=True)
        url = f'/admin/catalog/node/{node.pk}/actions/drain/'
        data = {'expected_revision': '1', 'request_id': str(uuid4()), 'epoch': str(self.installation.epoch),
                'not_after': (timezone.now()+timedelta(minutes=2)).isoformat()}
        # Composition объявляет только реально связанный finite handler; никакого fake submit.
        with patch('mop.server.controller.adminservices.node_actions', return_value=('drain', 'enable')):
            response = self.post(url, data)
            self.assertEqual(response.status_code, 302)
            operation = o.Operation.objects.get()
            self.assertIn(str(operation.pk), response['Location'])
            self.assertEqual(self.post(url, data)['Location'], response['Location'])
            conflict = self.post(url, {**data, 'expected_revision': '2'})
            self.assertEqual(conflict.status_code, 409)
        self.assertEqual(o.Operation.objects.count(), 1)
        node.refresh_from_db()
        self.assertEqual((node.revision, node.desired_eligible), (1, True))
        self.assertEqual(operation.state, 'accepted')
        self.assertEqual(o.AuditEvent.objects.count(), 1)
        self.assertEqual(o.OutboxEvent.objects.count(), 1)
        # Binding fixture подкреплён настоящим #625 Worker, не fake submit.
        # Контролируемый внешний adapter запускается только после HTTP ответа.
        from asgiref.sync import async_to_sync
        from mop.server.controller.runtime import Runtime, operations_service
        from mop.server.operations.worker import Worker
        token = access.enroll_service('admin-node-executor', node.pk, project_ids=(),
            permissions=('catalog.record_node_application', 'catalog.observe_node'))
        executor = access.token_context(token)
        calls = []
        class NodeExecutor:
            async def set_eligibility(inner, claim):
                calls.append(claim)
                return True
        async def consume():
            runtime = Runtime(database_limit=2, queue_limit=0)
            worker = Worker(runtime, operations_service(executor=executor), NodeExecutor())
            try:
                await worker.run_one(operation.pk)
            finally:
                await runtime.aclose()
        async_to_sync(consume)()
        operation.refresh_from_db()
        node.refresh_from_db()
        self.assertEqual(len(calls), 1)
        self.assertEqual(operation.state, 'succeeded')
        self.assertEqual((node.revision, node.desired_eligible, node.observed_eligible), (2, False, False))
        self.assertEqual(o.EffectReceipt.objects.count(), 1)
        with patch('mop.server.controller.adminservices.node_actions', return_value=('drain', 'enable')):
            response = self.post(f'/admin/catalog/node/{node.pk}/actions/enable/',
                {**data, 'expected_revision': '2', 'request_id': str(uuid4())})
            self.assertEqual(response.status_code, 302)
        operation = o.Operation.objects.exclude(pk=operation.pk).get()
        self.assertEqual((operation.action, operation.eligible, operation.state), ('enable', True, 'accepted'))
        node.refresh_from_db()
        self.assertFalse(node.observed_eligible)
        async_to_sync(consume)()
        operation.refresh_from_db()
        node.refresh_from_db()
        self.assertEqual((operation.state, node.revision, node.observed_eligible), ('succeeded', 3, True))
        self.assertEqual(o.EffectReceipt.objects.count(), 2)

    def test_replay_after_action_downgrade_uses_common_query_authority_not_a_new_admin_gate(self):
        self.global_scope()
        self.allow('catalog.view_node', 'catalog.drain_node', 'operations.view_operation')
        node = self.node()
        Installation.objects.update(effects_enabled=True)
        url = f'/admin/catalog/node/{node.pk}/actions/drain/'
        data = {'expected_revision': '1', 'request_id': str(uuid4()), 'epoch': str(self.installation.epoch),
                'not_after': (timezone.now()+timedelta(minutes=2)).isoformat()}
        with patch('mop.server.controller.adminservices.node_actions', return_value=('drain', 'enable')):
            first = self.post(url, data)
            self.assertEqual(first.status_code, 302)
            self.user.user_permissions.remove(*Permission.objects.filter(content_type__app_label='catalog',
                codename__in=('drain_node', 'view_node')))
            response = self.post(url, data)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response['Location'], first['Location'])
            rest = self.client.post(f'/api/v1/nodes/{node.pk}/commands/drain',
                {'schema_version': 1, **data, 'params': {}}, content_type='application/json', secure=True)
            self.assertEqual(rest.status_code, 202)
            self.assertEqual(rest.json()['operation']['id'], str(o.Operation.objects.get().pk))
            self.assertEqual(self.post(url, {**data, 'request_id': str(uuid4())}).status_code, 403)
        self.assertEqual(o.Operation.objects.count(), 1)
        self.assertEqual(o.AuditEvent.objects.count(), 1)

    def test_secret_metadata_and_replacement_never_echo_or_log_bytes(self):
        self.allow('secrets.view_secret_metadata', 'secrets.manage_project_secret')
        secret = s.Secret.objects.create(installation=self.installation, project=self.project,
            kind='variable', name='TEST_TOKEN', purpose='project')
        self.assertFalse(admin.site.is_registered(s.SecretVersion))
        response = self.get(f'/admin/secrets/secret/{secret.pk}/change/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'active_version')
        marker = 'synthetic-secret-admin-638'
        url = f'/admin/secrets/secret/{secret.pk}/replace/'
        response = self.post(url, {'value': marker, 'expected_revision': '1'})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(marker, response.content.decode())
        self.assertFalse(LogEntry.objects.filter(change_message__contains=marker).exists())
        self.assertFalse(LogEntry.objects.filter(object_repr__contains=marker).exists())

    def test_secret_replacement_uses_real_rootless_socket_and_logs_metadata_only(self):
        import asyncio
        import os
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from asgiref.sync import async_to_sync
        from django.conf import settings
        from mop.common.credential_client import CredentialClient
        from mop.common.credentials import Credential
        from mop.server.controller.credentials import application, CredentialService
        from mop.server.controller.runtime import Runtime
        self.allow('secrets.view_secret_metadata', 'secrets.manage_project_secret')
        # Root находится только в fixture credential application, не в web adapter.
        with override_settings(BOOTSTRAP={**settings.BOOTSTRAP, 'role': 'credential'}):
            protected = application()
        auth = Credential('session', self.client.session.session_key)
        marker = 'secret-replacement-private-marker-638'
        async def exercise(path):
            runtime = Runtime(database_limit=2, queue_limit=0)
            server = CredentialService(path, application=protected, runtime=runtime, allowed_uids=(os.geteuid(),))
            await server.start()
            try:
                client = CredentialClient(path, server_uid=os.geteuid())
                item = await asyncio.to_thread(client.create, auth, project_id=self.project.pk,
                    name='REPLACE_TOKEN', kind='variable', purpose='project', value=b'synthetic-old-secret')
                url = f'/admin/secrets/secret/{item.id}/replace/'
                with patch('mop.server.controller.adminservices.credential_client', return_value=client), \
                     patch('mop.server.controller.bootstrap.secret_provider',
                           side_effect=AssertionError('web must not construct a root provider')):
                    response = await asyncio.to_thread(self.post, url, {'value': marker, 'expected_revision': '1'})
                self.assertEqual(response.status_code, 302)
                self.secret_id = item.id
            finally:
                await server.aclose()
                await runtime.aclose()
        with TemporaryDirectory(prefix='mop-admin-638-') as directory:
            async_to_sync(exercise)(str(Path(directory) / 'credential.sock'))
        row = s.Secret.objects.get(pk=self.secret_id)
        self.assertEqual((row.revision, row.last_number), (2, 2))
        self.assertNotIn(marker.encode(), bytes(s.SecretVersion.objects.get(secret=row, number=2).envelope))
        self.assertTrue(LogEntry.objects.filter(object_id=str(row.pk)).exists())
        for entry in LogEntry.objects.all():
            self.assertNotIn(marker, entry.change_message + entry.object_repr)
            self.assertNotIn(auth.value, entry.change_message + entry.object_repr)

    def test_readonly_evidence_denies_all_bulk_inline_and_model_mutations(self):
        from mop.server.access.models import RoleBundle
        for model in (RoleBundle, s.SecretAudit, s.CredentialRotation, o.AuditEvent, o.EffectReceipt, c.NodeImage):
            self.assertTrue(admin.site.is_registered(model), model._meta.label)
            adapter = admin.site._registry[model]
            request = self.get('/admin/').wsgi_request
            self.assertFalse(adapter.has_add_permission(request))
            self.assertFalse(adapter.has_change_permission(request))
            self.assertFalse(adapter.has_delete_permission(request))
            self.assertEqual(adapter.get_actions(request), {})
            self.assertEqual(adapter.get_inline_instances(request), [])

    def test_nonhuman_target_cannot_gain_password_staff_or_permission_via_user_admin(self):
        self.global_scope()
        self.allow('auth.view_user', 'auth.change_user')
        node = self.node()
        access.enroll_service('machine', node.pk, project_ids=(), permissions=('catalog.observe_node',))
        target = get_user_model().objects.get(username='machine')
        url = f'/admin/actor/actor/{target.pk}/change/'
        response = self.post(url, {'username': 'machine', 'is_staff': 'on', 'is_superuser': 'on', '_save': 'Save'})
        self.assertIn(response.status_code, (302, 403, 404))
        target.refresh_from_db()
        self.assertFalse(target.is_staff or target.is_superuser or target.has_usable_password())
        self.assertEqual(self.post(f'/admin/actor/actor/{target.pk}/password/',
            {'password1': 'new-test-password', 'password2': 'new-test-password'}).status_code, 403)

    def test_retired_project_and_node_do_not_rescope_admin_queries(self):
        self.global_scope()
        self.allow('catalog.view_node')
        c.Project.objects.filter(pk=self.project.pk).update(retired_at=timezone.now(), identity_slot=self.project.pk)
        node = self.node()
        c.Node.objects.filter(pk=node.pk).update(retired_at=timezone.now(), identity_slot=node.pk)
        self.assertNotContains(self.get('/admin/catalog/project/'), self.project.origin)
        self.assertEqual(self.get('/admin/catalog/node/').context['cl'].result_count, 0)

    def test_secret_foreign_scope_is_filtered_for_list_count_detail_and_replacement(self):
        self.allow('secrets.view_secret_metadata', 'secrets.manage_project_secret')
        own = s.Secret.objects.create(installation=self.installation, project=self.project,
            kind='variable', name='OWN_TOKEN', purpose='project')
        foreign = s.Secret.objects.create(installation=self.installation, project=self.foreign,
            kind='variable', name='FOREIGN_TOKEN', purpose='project')
        response = self.get('/admin/secrets/secret/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['cl'].result_count, 1)
        self.assertNotContains(response, foreign.name)
        for verb in ('change', 'replace'):
            response = self.get(f'/admin/secrets/secret/{foreign.pk}/{verb}/')
            self.assertIn(response.status_code, (302, 404))
        a.ProjectMembership.objects.filter(user=self.user).delete()
        self.assertEqual(self.get('/admin/secrets/secret/').context['cl'].result_count, 0)
        response = self.post(f'/admin/secrets/secret/{own.pk}/replace/',
            {'value': 'private-rejected-value', 'expected_revision': '1'})
        self.assertEqual(response.status_code, 404)
        own.refresh_from_db()
        self.assertEqual(own.last_number, 0)

    def test_node_settings_target_is_rechecked_after_permission_downgrade(self):
        self.global_scope()
        self.allow('catalog.view_node', 'catalog.view_nodesetting', 'catalog.change_nodesetting', 'catalog.configure_node')
        node = self.node()
        url = '/admin/catalog/nodesetting/configure/?node=' + str(node.pk)
        self.assertEqual(self.get(url).status_code, 200)
        self.user.user_permissions.clear()
        self.assertEqual(self.post(url, {'expected_revision': '1', 'node': str(node.pk), 'log_level': 'warning'}).status_code, 403)
        self.assertFalse(c.NodeSetting.objects.exists())

    def test_catalogue_validation_reason_is_not_sanitized_a_second_time(self):
        self.global_scope()
        self.allow('catalog.add_node', 'catalog.view_node', 'catalog.change_node', 'catalog.configure_node')
        # ABANDONED: invalid driver останавливает ModelForm constraint, не Catalog.
        # Host path проходит CharField и проверяется именно публичным Catalog.
        response = self.post('/admin/catalog/node/add/', {'name': 'invalid', 'driver': 'host',
            'ssh_host': 'node.invalid/path', 'ssh_port': '22', 'control_origin': 'https://node.invalid',
            'lan_address': 'node.invalid', 'bootstrap_address': 'node.invalid', '_save': 'Save'})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'invalid node address', status_code=400)
        self.assertFalse(c.Node.objects.exists())

    def test_safe_secret_storage_failure_keeps_its_reason_in_html(self):
        from mop.server.secrets.services import StorageUnavailable
        self.allow('secrets.view_secret_metadata', 'secrets.manage_project_secret')
        secret = s.Secret.objects.create(installation=self.installation, project=self.project,
            kind='variable', name='UNAVAILABLE_TOKEN', purpose='project')
        with patch('mop.server.secrets.services.SecretStore.metadata',
                   side_effect=StorageUnavailable('secret storage is unavailable; maintenance required')):
            response = self.post(f'/admin/secrets/secret/{secret.pk}/replace/',
                                 {'value': 'do-not-echo-638', 'expected_revision': '1'})
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, 'secret storage is unavailable; maintenance required', status_code=503)
        self.assertNotContains(response, 'do-not-echo-638', status_code=503)

    def test_gateway_permissions_are_real_model_metadata_and_token_admin_stays_absent(self):
        from rest_framework.authtoken.models import Token, TokenProxy
        self.assertTrue(Permission.objects.filter(content_type__app_label='controller', codename='manage_proxy').exists())
        self.assertFalse(admin.site.is_registered(Token))
        self.assertFalse(admin.site.is_registered(TokenProxy))

    def test_admin_recent_log_sidebar_cannot_bypass_current_project_scope(self):
        self.global_scope()
        self.allow('catalog.change_project', 'catalog.configure_project', 'catalog.set_project_limit')
        response = self.post(f'/admin/catalog/project/{self.project.pk}/change/', self.project_form())
        self.assertEqual(response.status_code, 302)
        self.assertTrue(LogEntry.objects.filter(object_id=str(self.project.pk)).exists())
        a.AllProjectMembership.objects.filter(user=self.user).delete()
        a.ProjectMembership.objects.filter(user=self.user).delete()
        self.assertNotContains(self.get('/admin/'), str(self.project.pk))

    # HYPOTHESIS: manage_proxy зарегистрирован у DomainPermission и Installation;
    # положительная фикстура должна выбирать ContentType, не случайную строку.
    # SOLUTION: natural key DomainPermission; отзыв удаляет оба grants действия,
    # сохраняя независимое view_project и проверку отсутствующей gateway-ссылки.
    # RESULT: положительная фикстура выбирает зарегистрированный ContentType;
    # отзыв логического действия скрывает ссылку без очистки прочих разрешений.
    # STATUS: FIXED — see #638 (только интеграционная фикстура).
    def test_admin_fixture_grants_registered_controller_permission(self):
        self.allow('controller.manage_proxy')
        self.assertEqual(list(self.user.user_permissions.filter(
            content_type__app_label='controller').values_list('content_type__model', 'codename')),
            [('domainpermission', 'manage_proxy')])

    def test_authorized_admin_index_links_separate_management_gateway(self):
        self.global_scope()
        self.allow('controller.manage_proxy')
        response = self.get('/admin/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '/llm/management.html')
        self.user.user_permissions.add(Permission.objects.get_by_natural_key(
            'manage_proxy', 'controller', 'installation'))
        self.assertEqual(self.user.user_permissions.filter(
            content_type__app_label='controller', codename='manage_proxy').count(), 2)
        self.user.user_permissions.remove(*Permission.objects.filter(
            content_type__app_label='controller', codename='manage_proxy'))
        self.assertNotContains(self.get('/admin/'), '/llm/management.html')
        self.assertTrue(self.user.user_permissions.filter(
            content_type__app_label='catalog', codename='view_project').exists())
