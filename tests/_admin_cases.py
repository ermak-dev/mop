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
            if app == 'auth':  # logical namespace; rows live under actor label
                code = code.replace('_user', '_actor', 1)
                permission = Permission.objects.get(content_type__app_label='actor', codename=code)
            else:  # единственный канонический владелец на codename (#685)
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
        from mop.server.access.models import AgentDefinitionVersion
        for model in (AgentDefinitionVersion, s.SecretAudit, s.CredentialRotation, o.AuditEvent, o.EffectReceipt, c.NodeImage):
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

    # HYPOTHESIS: manage_proxy зарегистрирован у Installation и DomainPermission;
    # положительная фикстура должна выбирать ContentType, не случайную строку.
    # SOLUTION: единый канонический владелец Installation (#685), ровно одна
    # строка registration: её отзыв скрывает ссылку без очистки прочих прав.
    # RESULT: положительная фикстура выбирает зарегистрированный ContentType;
    # отзыв логического действия скрывает ссылку без очистки прочих разрешений.
    # STATUS: FIXED — see #638; единственный владелец с #685.
    def test_admin_fixture_grants_registered_controller_permission(self):
        self.allow('controller.manage_proxy')
        self.assertEqual(list(self.user.user_permissions.filter(
            content_type__app_label='controller').values_list('content_type__model', 'codename')),
            [('installation', 'manage_proxy')])

    def test_authorized_admin_index_links_separate_management_gateway(self):
        self.global_scope()
        self.allow('controller.manage_proxy')
        response = self.get('/admin/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '/llm/management.html')
        # Ровно одна registration-строка: её отзыв закрывает действие.
        self.assertEqual(self.user.user_permissions.filter(
            content_type__app_label='controller', codename='manage_proxy').count(), 1)
        self.user.user_permissions.remove(*Permission.objects.filter(
            content_type__app_label='controller', codename='manage_proxy'))
        self.assertNotContains(self.get('/admin/'), '/llm/management.html')
        self.assertTrue(self.user.user_permissions.filter(
            content_type__app_label='catalog', codename='view_project').exists())


# FEATURE: #756 — обычный Django RBAC nonhuman Actor без generic identity CRUD.
# HYPOTHESIS: queryset целиком скрывает agent/service; простое снятие фильтра
# откроет UserAdmin password/staff и silently ignored protected POST fields.
# SPEC: реальные PG17 GET/POST, общий Access и named membership owner; stable
# Actor/Token/identity/enrollment/profile/delegation, LDAP source ownership.
# RED: PG17/Python3.12 — список не содержит agent/service, GET/POST дают 302;
# protected/add forgeries — 302 вместо 403, membership owner отсутствует.
# SOLUTION: узкая nonhuman ModelForm + общий ActorMemberships с fresh Access;
# canonical поля readonly, неизвестные POST отвергаются, LDAP owner сохранён.
# RED (дополнительно): новый filtered values_list после validation молча
# терял ставший managed Group и принимал POST; передаём уже проверенные IDs.
# RESULT: PG17/Python3.12 — 16 новых request/application checks и все прежние
# Admin/Auth/RBAC/LDAP/provisioning checks: 182/182; fixture UID gate 5/5.
# STATUS: FIXED — see #756. Полный strict CI — gate интеграции на точном SHA.
@override_settings(ALLOWED_HOSTS=['testserver'])
class NonhumanMembershipAdmin(TransactionTestCase):
    serialized_rollback = True
    def allow(self, *names):
        from _grant_cases import permit_actions
        permit_actions(self.user, *names)

    global_scope = OperationBackedAdmin.global_scope
    post = OperationBackedAdmin.post
    get = OperationBackedAdmin.get
    node = OperationBackedAdmin.node

    def setUp(self):
        OperationBackedAdmin.setUp(self)
        self.global_scope()
        self.allow('auth.view_user', 'auth.change_user', 'auth.add_user',
            'auth.view_group', 'auth.change_group', 'access.view_role', 'access.approve_role',
            'access.issue_grant', 'access.revoke_grant', 'secrets.manage_project_secret',
            'execution.create_puppet', 'catalog.view_pool')
        self.human = self.user
        self.actor = access.token_context(access.issue_token(self.user.pk))
        self.operator = access.session_context(self.client.session.session_key)

    def targets(self):
        if hasattr(self, '_targets'):
            return self._targets
        from _actor_auth_cases import _grant_scaffolding
        from rest_framework.authtoken.models import Token
        self.api, reserved = _grant_scaffolding(self)
        provisioned = self.api.provision(self.actor, reserved.id, expected_revision=reserved.revision)
        provisioned = self.api.get(self.actor, reserved.id)
        self.api.bindings.bindings[(self.project.pk, provisioned.target_id)] = provisioned.binding
        self.api.bindings.receipts[provisioned.id] = provisioned.binding
        self.api.activate(provisioned.id, expected_revision=provisioned.revision)
        self.grant = a.Grant.objects.get(pk=reserved.id)
        agent = get_user_model().objects.get(pk=reserved.subject_id)
        self.agent_key = Token.objects.get(user=agent).key
        self.agent_context = access.token_context(self.agent_key, grants=self.api.local_authority())
        self.service_node = self.node()
        c.NodeProject.objects.create(node=self.service_node, project=self.project)
        self.service_key = access.enroll_service('admin-service-756', self.service_node.pk,
            project_ids=(self.project.pk,), permissions=('catalog.observe_node',))
        service = get_user_model().objects.get(username='admin-service-756')
        self.service_context = access.token_context(self.service_key)
        self._targets = (agent, service)
        return self._targets

    @staticmethod
    def url(target):
        return f'/admin/actor/actor/{target.pk}/change/'

    @staticmethod
    def form(**changes):
        return {'groups': [], 'user_permissions': [], '_save': 'Save', **changes}

    @staticmethod
    def permissions(*codes):
        return list(Permission.objects.filter(content_type__app_label='catalog', codename__in=codes).order_by('pk'))

    @staticmethod
    def stable():
        from rest_framework.authtoken.models import Token
        # Полные synthetic rows: сравниваем также generation/enrollment/lifecycle,
        # а не только число субъектов. В сообщениях и LogEntry этих bytes нет.
        return [list(model.objects.order_by('pk').values()) for model in
            (get_user_model(), Token, a.LoginIdentity, a.CredentialBinding, a.ServiceEnrollment,
             a.AgentProfile, a.Grant, a.AgentRBACProvisioning, a.CredentialEvent)]

    def memberships(self):
        return [(list(row.groups.order_by('pk').values_list('pk', flat=True)),
                 list(row.user_permissions.order_by('pk').values_list('pk', flat=True)))
                for row in self.targets()]

    def test_global_operator_lists_and_edits_agent_and_service_groups_and_direct_permissions(self):
        from django.contrib.auth.models import Group
        group = Group.objects.create(name='manual-rbac-756')
        group.permissions.add(*self.permissions('view_pool'))
        direct = self.permissions('view_project')[0]
        targets = self.targets()
        before = self.stable()
        response = self.get('/admin/actor/actor/')
        self.assertEqual(response.status_code, 200)
        for target in targets:
            with self.subTest(kind=target.kind):
                self.assertContains(response, target.username)
                page = self.get(self.url(target))
                self.assertEqual(page.status_code, 200)
                self.assertEqual(set(page.context['adminform'].form.fields), {'groups', 'user_permissions'})
                self.assertContains(page, target.username)
                self.assertContains(page, target.kind)
                self.assertContains(page, 'does not stop')
                self.assertContains(page, 'does not delete')
                self.assertNotContains(page, 'id_password')
                for marker in (self.agent_key, self.service_key, target.password, 'token_digest', 'credential_version'):
                    self.assertNotIn(marker, page.content.decode())
                saved = self.post(self.url(target), self.form(groups=[str(group.pk)], user_permissions=[str(direct.pk)]))
                self.assertEqual(saved.status_code, 302)
                self.assertEqual(list(target.groups.values_list('pk', flat=True)), [group.pk])
                self.assertEqual(list(target.user_permissions.values_list('pk', flat=True)), [direct.pk])
        self.assertEqual(self.stable(), before)
        entries = LogEntry.objects.filter(object_id__in=[str(row.pk) for row in targets])
        self.assertEqual(entries.count(), 2)
        for entry in entries:
            self.assertIn('groups', entry.change_message)
            for marker in (self.agent_key, self.service_key, 'token_digest', 'credential_version'):
                self.assertNotIn(marker, entry.change_message + entry.object_repr)

    def test_view_only_global_operator_sees_readonly_nonhuman_but_cannot_mutate(self):
        targets = self.targets()
        self.user.user_permissions.remove(*Permission.objects.filter(content_type__app_label='actor', codename='change_actor'))
        before = self.stable(), self.memberships()
        for target in targets:
            page = self.get(self.url(target))
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, 'name="groups"')
            self.assertEqual(self.post(self.url(target), self.form()).status_code, 403)
        self.assertEqual((self.stable(), self.memberships()), before)

    def test_scoped_or_permissionless_staff_cannot_list_read_or_change_nonhuman(self):
        targets = self.targets()
        before = self.stable(), self.memberships()
        for downgrade in ('scope', 'permission'):
            with self.subTest(downgrade=downgrade):
                if downgrade == 'scope':
                    a.AllProjectMembership.objects.filter(user=self.user).delete()
                else:
                    self.global_scope()
                    self.user.user_permissions.clear()
                self.assertEqual(self.get('/admin/actor/actor/').status_code, 403)
                for target in targets:
                    self.assertEqual(self.get(self.url(target)).status_code, 403)
                    self.assertEqual(self.post(self.url(target), self.form()).status_code, 403)
        self.assertEqual((self.stable(), self.memberships()), before)

    def test_permission_revoke_and_scope_downgrade_between_get_and_post_deny_unchanged(self):
        targets = self.targets()
        before = self.stable(), self.memberships()
        for target in targets:
            for downgrade in ('scope', 'permission'):
                self.global_scope()
                self.allow('auth.change_user')
                self.assertEqual(self.get(self.url(target)).status_code, 200)
                if downgrade == 'scope':
                    a.AllProjectMembership.objects.filter(user=self.user).delete()
                else:
                    self.user.user_permissions.remove(*Permission.objects.filter(
                        content_type__app_label='actor', codename='change_actor'))
                self.assertEqual(self.post(self.url(target), self.form()).status_code, 403)
        self.assertEqual((self.stable(), self.memberships()), before)

    def test_every_nonhuman_protected_field_forgery_is_denied_not_silently_accepted(self):
        from django.contrib.auth.models import Group
        targets = self.targets()
        group = Group.objects.create(name='forged-mixed-write-756')
        direct = self.permissions('view_project')[0]
        before = self.stable(), self.memberships()
        fields = ('id', 'pk', 'username', 'kind', 'password', 'password1', 'password2',
            'usable_password', 'old_password', 'is_staff', 'is_superuser', 'is_active',
            'first_name', 'last_name', 'email', 'last_login', 'date_joined', 'identity',
            'loginidentity', 'source', 'external_subject', 'authorized', 'validated_at',
            'service_enrollment', 'node', 'use_case', 'agent_profile', 'owner', 'project',
            'target_id', 'definition', 'revision', 'token', 'key', 'credentialbinding',
            'token_digest', 'grant', 'state', 'retired_at', 'unknown_protected_field')
        for target in targets:
            for name in fields:
                with self.subTest(kind=target.kind, field=name):
                    self.assertEqual(self.post(self.url(target), self.form(groups=[str(group.pk)],
                        user_permissions=[str(direct.pk)], **{name: 'forged-756'})).status_code, 403)
                    self.assertEqual((self.stable(), self.memberships()), before)
        self.assertFalse(LogEntry.objects.exists())

    def test_directory_managed_group_injection_is_denied_and_owned_membership_preserved(self):
        from django.contrib.auth.models import Group
        ordinary = Group.objects.create(name='ordinary-756')
        managed = Group.objects.create(name='directory-managed-756')
        source = a.IdentitySource.objects.create(name='directory-756', kind='ldap')
        a.DirectoryGroupRule.objects.create(source=source, group_dn='cn=managed', group=managed)
        for target in self.targets():
            before = self.stable(), self.memberships()
            response = self.post(self.url(target), self.form(groups=[str(ordinary.pk), str(managed.pk)]))
            self.assertIn(response.status_code, (400, 403))
            self.assertEqual((self.stable(), self.memberships()), before)
            # Даже уже существующее source-owned участие не принадлежит форме.
            target.groups.add(managed)
            page = self.get(self.url(target))
            self.assertEqual(page.status_code, 200)
            self.assertNotIn(managed.pk, page.context['adminform'].form.fields['groups'].queryset.values_list('pk', flat=True))
            self.assertEqual(self.post(self.url(target), self.form(groups=[str(ordinary.pk)])).status_code, 302)
            self.assertEqual(set(target.groups.values_list('pk', flat=True)), {ordinary.pk, managed.pk})

    def test_nonhuman_password_delete_bulk_inline_and_save_as_new_paths_deny_unchanged(self):
        targets = self.targets()
        before = self.stable(), self.memberships()
        for target in targets:
            root = f'/admin/actor/actor/{target.pk}/'
            self.assertEqual(self.get(root + 'password/').status_code, 403)
            self.assertEqual(self.post(root + 'password/', {'password1': 'forged-password',
                'password2': 'forged-password'}).status_code, 403)
            self.assertEqual(self.post(root + 'delete/', {'post': 'yes'}).status_code, 403)
            for data in ({'_saveasnew': 'Save as new'}, {'action': 'delete_selected'},
                         {'actor_groups-TOTAL_FORMS': '1', 'actor_groups-INITIAL_FORMS': '0'}):
                self.assertEqual(self.post(self.url(target), self.form(**data)).status_code, 403)
            self.assertEqual(self.post('/admin/actor/actor/', {'action': 'delete_selected',
                '_selected_action': [str(target.pk)], 'post': 'yes'}).status_code, 403)
        self.assertEqual((self.stable(), self.memberships()), before)

    def test_forged_nonhuman_add_and_group_reverse_membership_are_denied(self):
        from django.contrib.auth.models import Group
        targets = self.targets()
        before = self.stable(), self.memberships()
        for kind in ('agent', 'service'):
            response = self.post('/admin/actor/actor/add/', {'username': 'forged-' + kind, 'kind': kind,
                'password1': 'test-only-password-756', 'password2': 'test-only-password-756', '_save': 'Save'})
            self.assertEqual(response.status_code, 403)
        group = Group.objects.create(name='reverse-756')
        for name in ('user_set', 'users', 'actor_set', 'user_ids'):
            self.assertEqual(self.post(f'/admin/auth/group/{group.pk}/change/',
                {'name': group.name, name: [str(row.pk) for row in targets], '_save': 'Save'}).status_code, 403)
        self.assertEqual((self.stable(), self.memberships()), before)

    def test_standard_human_change_and_ldap_readonly_behavior_are_preserved(self):
        from django.contrib.auth.models import Group
        human = get_user_model().objects.create_user('local-human-756', password='test-only-password')
        access.bind_local_identity(human.pk)
        ordinary = Group.objects.create(name='human-ordinary-756')
        direct = self.permissions('view_project')[0]
        response = self.post(self.url(human), {'username': human.username, 'first_name': 'Edited',
            'last_name': 'Human', 'email': 'human@example.invalid', 'is_active': 'on',
            'date_joined_0': human.date_joined.strftime('%Y-%m-%d'), 'date_joined_1': human.date_joined.strftime('%H:%M:%S'),
            'groups': [str(ordinary.pk)], 'user_permissions': [str(direct.pk)], '_save': 'Save'})
        self.assertEqual(response.status_code, 302,
            response.context['adminform'].form.errors if response.status_code == 200 else '')
        human.refresh_from_db()
        self.assertEqual(human.first_name, 'Edited')
        self.assertEqual(list(human.groups.values_list('pk', flat=True)), [ordinary.pk])
        self.assertEqual(list(human.user_permissions.values_list('pk', flat=True)), [direct.pk])
        source = a.IdentitySource.objects.create(name='ldap-human-756', kind='ldap')
        ldap = get_user_model().objects.create_user('ldap-human-756', password=None)
        a.LoginIdentity.objects.create(user=ldap, source=source, external_subject='ldap-subject-756')
        page = self.get(self.url(ldap))
        self.assertEqual(page.status_code, 200)
        for field in ('username', 'groups', 'user_permissions'):
            self.assertNotIn(field, page.context['adminform'].form.fields)
        self.assertEqual(self.post(f'/admin/actor/actor/{ldap.pk}/password/',
            {'password1': 'forged-password', 'password2': 'forged-password'}).status_code, 403)
        ldap.refresh_from_db()
        self.assertFalse(ldap.has_usable_password())
        self.assertEqual(self.post(f'/admin/actor/actor/{human.pk}/password/',
            {'password1': 'another-test-only-password', 'password2': 'another-test-only-password',
             'usable_password': 'true'}).status_code, 302)
        human.refresh_from_db()
        self.assertTrue(human.check_password('another-test-only-password'))

    def test_memberships_change_fresh_access_not_token_identity_or_delegation_bounds(self):
        from django.contrib.auth.models import Group
        agent, service = self.targets()
        before = self.stable()
        group = Group.objects.create(name='live-rbac-756')
        group.permissions.add(*self.permissions('view_pool', 'observe_node'))
        direct = self.permissions('record_node_application')[0]
        for target in (agent, service):
            self.assertEqual(self.post(self.url(target), self.form(groups=[str(group.pk)],
                user_permissions=[str(direct.pk)])).status_code, 302)
        delegated = access.Access(grants=self.api.local_authority())
        delegated.authorize(self.agent_context, 'catalog.view_pool', project_ids=(self.project.pk,))
        access.Access().authorize(self.service_context, 'catalog.observe_node',
            project_ids=(self.project.pk,), node_ids=(self.service_node.pk,))
        access.Access().authorize(self.service_context, 'catalog.record_node_application',
            project_ids=(self.project.pk,), node_ids=(self.service_node.pk,))
        # Широкая membership субъекта не обходит human issuer или foreign scope.
        permission = self.permissions('view_pool')[0]
        self.user.user_permissions.remove(permission)
        with self.assertRaises(access.Denied):
            delegated.authorize(self.agent_context, 'catalog.view_pool', project_ids=(self.project.pk,))
        self.user.user_permissions.add(permission)
        with self.assertRaises(access.Denied):
            delegated.authorize(self.agent_context, 'catalog.view_pool', project_ids=(self.foreign.pk,))
        for target in (agent, service):
            self.assertEqual(self.post(self.url(target), self.form()).status_code, 302)
        for owner, context, action, refs in (
            (delegated, self.agent_context, 'catalog.view_pool', {'project_ids': (self.project.pk,)}),
            (access.Access(), self.service_context, 'catalog.observe_node',
                {'project_ids': (self.project.pk,), 'node_ids': (self.service_node.pk,)}),
            (access.Access(), self.service_context, 'catalog.record_node_application',
                {'project_ids': (self.project.pk,), 'node_ids': (self.service_node.pk,)})):
            with self.assertRaises(access.Denied):
                owner.authorize(context, action, **refs)
        self.assertEqual(self.stable(), before)
        self.assertEqual(a.Grant.objects.get(pk=self.grant.pk).state, 'active')

    def test_change_user_alone_with_global_scope_is_sufficient_without_recipe_or_group_admin_rights(self):
        from django.contrib.auth.models import Group
        targets = self.targets()
        self.user.user_permissions.set(Permission.objects.filter(content_type__app_label='actor', codename='change_actor'))
        group = Group.objects.create(name='change-user-only-756')
        for target in targets:
            self.assertEqual(self.get(self.url(target)).status_code, 200)
            self.assertEqual(self.post(self.url(target), self.form(groups=[str(group.pk)])).status_code, 302)
            self.assertEqual(list(target.groups.values_list('pk', flat=True)), [group.pk])

    def test_application_owner_revalidates_after_admin_form_validation(self):
        from django.contrib.auth.models import Group
        from mop.server.controller.admin import ScopedUserAdmin
        targets = self.targets()
        original = ScopedUserAdmin.save_model
        group = Group.objects.create(name='late-validation-756')
        source = a.IdentitySource.objects.create(name='late-directory-756', kind='ldap')
        for target in targets:
            for downgrade in ('permission', 'scope', 'directory'):
                with self.subTest(kind=target.kind, downgrade=downgrade):
                    self.allow('auth.change_user')
                    self.global_scope()
                    a.DirectoryGroupRule.objects.filter(source=source).delete()
                    before = self.stable(), self.memberships()
                    calls = []
                    def revoked(adapter, request, obj, form, change):
                        calls.append(set(form.cleaned_data))
                        if downgrade == 'permission':
                            self.user.user_permissions.remove(*Permission.objects.filter(
                                content_type__app_label='actor', codename='change_actor'))
                        elif downgrade == 'scope':
                            a.AllProjectMembership.objects.filter(user=self.user).delete()
                        else:
                            a.DirectoryGroupRule.objects.create(source=source, group_dn='cn=late', group=group)
                        return original(adapter, request, obj, form, change)
                    with patch.object(ScopedUserAdmin, 'save_model', revoked):
                        response = self.post(self.url(target), self.form(groups=[str(group.pk)]))
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(calls, [{'groups', 'user_permissions'}])
                    self.assertEqual((self.stable(), self.memberships()), before)
        self.assertFalse(LogEntry.objects.exists())

    def test_ldap_agent_memberships_remain_source_owned_and_not_generic_admin_mutation(self):
        from django.contrib.auth.models import Group
        agent, _ = self.targets()
        source = a.IdentitySource.objects.create(name='agent-directory-756', kind='ldap')
        a.LoginIdentity.objects.filter(user=agent).update(source=source, validated_revision=source.revision,
            validated_at=timezone.now())
        managed = Group.objects.create(name='agent-managed-756')
        a.DirectoryGroupRule.objects.create(source=source, group_dn='cn=agent', group=managed)
        agent.groups.add(managed)
        before = self.stable(), self.memberships()
        page = self.get(self.url(agent))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(set(page.context['adminform'].form.fields), set())
        for data in (self.form(), self.form(groups=[]), self.form(user_permissions=[])):
            self.assertEqual(self.post(self.url(agent), data).status_code, 403)
        self.assertEqual((self.stable(), self.memberships()), before)

    def test_revoked_delegation_stays_revoked_after_explicit_subject_rbac_edit(self):
        from django.contrib.auth.models import Group
        from rest_framework.authtoken.models import Token
        agent, _ = self.targets()
        group = Group.objects.create(name='revoked-delegation-756')
        group.permissions.add(*self.permissions('view_pool'))
        self.api.revoke(self.actor, self.grant.pk, expected_revision=self.grant.revision)
        self.assertEqual(self.post(self.url(agent), self.form(groups=[str(group.pk)])).status_code, 302)
        self.assertEqual(Token.objects.get(user=agent).key, self.agent_key)
        self.assertEqual(a.Grant.objects.get(pk=self.grant.pk).state, 'revoked')
        with self.assertRaises(access.Denied):
            access.Access(grants=self.api.local_authority()).authorize(self.agent_context,
                'catalog.view_pool', project_ids=(self.project.pk,))
        with self.assertRaises(access.Denied):
            access.token_context(self.agent_key, grants=self.api.local_authority())

    def test_standard_group_permission_edit_stays_available_without_reverse_membership(self):
        from django.contrib.auth.models import Group
        group = Group.objects.create(name='standard-group-756')
        permission = self.permissions('view_pool')[0]
        self.assertEqual(self.post(f'/admin/auth/group/{group.pk}/change/',
            {'name': group.name, 'permissions': [str(permission.pk)], '_save': 'Save'}).status_code, 302)
        self.assertEqual(list(group.permissions.values_list('pk', flat=True)), [permission.pk])

    def test_named_application_owner_rechecks_saved_context_and_rejects_directory_groups(self):
        from mop.server.access import rbac
        from django.contrib.auth.models import Group
        targets = self.targets()
        self.assertTrue(hasattr(rbac, 'ActorMemberships'), 'named access membership owner is missing')
        owner = rbac.ActorMemberships()
        group = Group.objects.create(name='owner-756')
        direct = self.permissions('view_project')[0]
        before = self.stable()
        for target in targets:
            owner.replace(self.operator, target.pk, group_ids=[group.pk], permission_ids=[direct.pk])
            self.assertEqual(list(target.groups.values_list('pk', flat=True)), [group.pk])
        memberships = self.memberships()
        self.user.user_permissions.remove(*Permission.objects.filter(content_type__app_label='actor', codename='change_actor'))
        with self.assertRaises(access.Denied):
            owner.replace(self.operator, targets[0].pk, group_ids=[], permission_ids=[])
        self.allow('auth.change_user')
        a.AllProjectMembership.objects.filter(user=self.user).delete()
        with self.assertRaises(access.Denied):
            owner.replace(self.operator, targets[0].pk, group_ids=[], permission_ids=[])
        self.global_scope()
        source = a.IdentitySource.objects.create(name='owner-ldap-756', kind='ldap')
        a.DirectoryGroupRule.objects.create(source=source, group_dn='cn=owner', group=group)
        with self.assertRaises(access.Denied):
            owner.replace(self.operator, targets[0].pk, group_ids=[group.pk], permission_ids=[])
        self.assertEqual(self.memberships(), memberships)
        with self.assertRaises(access.Denied):
            owner.replace(self.operator, self.user.pk, group_ids=[], permission_ids=[])
        self.assertEqual(self.stable(), before)
