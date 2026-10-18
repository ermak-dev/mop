"""#626: конечный read-only audit через настоящий DRF/PostgreSQL, не старый bus.

HYPOTHESIS: mounted journal не имеет modern query; view_audit ошибочно global.
SPEC: текущие credential/RBAC/project/node/grant до LIMIT, opaque keyset без payload.
RED: PG17/DRF: до реализации маршрут вернул 404 вместо 200 (1 failure).
SOLUTION: существующий AuditEvent + live Access proof, SQL anti-join до LIMIT.
RED: новый чужой NodeProject во время SELECT дал 200 вместо 410 (1 failure).
SOLUTION: тот же SQL predicate повторно проверяет конечный набор после чтения.
RESULT: 18 настоящих DRF/PG17 проверок зелёные, включая concurrent revoke.
STATUS: FIXED — see #626.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.db import close_old_connections, connection
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from mop.server.access import models as a, services as auth
from mop.server.catalog.models import Project, Node, NodeProject
from mop.server.operations import models as m


@override_settings(ALLOWED_HOSTS=['testserver'])
class AuditQueries(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        from mop.server.controller.runtime import initialize
        self.epoch = UUID(initialize()['epoch'])
        self.source = a.IdentitySource.objects.create(name='local', kind='local')
        self.project = Project.objects.create(name='one', origin='https://example.invalid/one.git')
        self.other = Project.objects.create(name='two', origin='https://example.invalid/two.git')
        self.node = self.make_node('one')
        self.other_node = self.make_node('two')
        for node, project in ((self.node, self.project), (self.node, self.other), (self.other_node, self.project)):
            NodeProject.objects.create(node=node, project=project)
        self.user, self.token = self.reader('reader', self.project)
        self.actor = auth.token_context(self.token)
        self.client = Client(HTTP_AUTHORIZATION='Token '+self.token)

    def make_node(self, name):
        return Node.objects.create(name=name, driver='host', ssh_host='example.invalid',
            control_origin='https://example.invalid', lan_address='example.invalid', bootstrap_address='example.invalid')

    def reader(self, name, *projects, kind='human', permission=True):
        user = get_user_model().objects.create_user(name, kind=kind)
        a.LoginIdentity.objects.create(user=user, source=self.source, external_subject=name)
        for project in projects:
            a.ProjectMembership.objects.create(user=user, source=self.source, project=project)
        if permission:
            user.user_permissions.add(Permission.objects.get(content_type__app_label='operations', codename='view_audit'))
        return user, auth.issue_token(user.pk)

    def event(self, *, projects=None, node=None, action='restart', at=None, state='accepted', code='accepted', reason='command accepted', revision=1, operation=None):
        if operation is None:
            puppet = action not in ('drain', 'enable', 'build')
            operation = m.Operation.objects.create(actor=self.user, actor_binding={}, request_id=uuid4(),
                digest='a'*64, epoch=self.epoch, restore_generation=uuid4(), action=action,
                node=node or self.node, expected_revision=1, eligible=None if puppet else False,
                target_kind='puppet' if puppet else 'node', target_id=uuid4() if puppet else None,
                incarnation_id=uuid4() if puppet else None, not_after=timezone.now()+timedelta(minutes=1))
            operation.projects.set([self.project] if projects is None else projects)
        return m.AuditEvent.objects.create(operation=operation, revision=revision, target_revision=1,
            state=state, code=code, reason=reason, created_at=at or timezone.now())

    def get(self, query=None, *, client=None):
        return (client or self.client).get('/api/v1/audit', query or {}, secure=True)

    def page(self, query=None, *, client=None):
        response = self.get(query, client=client)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn('no-store', response.headers.get('Cache-Control', ''))
        return response.json()

    def api(self):
        from mop.server.operations.audit import AuditQueries
        return AuditQueries(epoch=lambda:self.epoch)

    def test_real_drf_safe_immutable_allowlist_and_coverage(self):
        event = self.event(action='send', state='refused', code='dispatch_declined', reason='target does not receive dispatch')
        m.MessageBinding.objects.create(operation=event.operation, ciphertext=b'ciphertext-private-command', priority='next')
        body = self.page()
        self.assertEqual(set(body), {'schema_version','epoch','results','next_cursor','coverage'})
        self.assertEqual((body['schema_version'], body['epoch'], body['coverage']), (1, str(self.epoch), ['operation_audit']))
        self.assertIsNone(body['next_cursor'])
        self.assertEqual(body['results'], [dict(id=str(event.pk), operation_id=str(event.operation_id),
            revision='1', created_at=event.created_at.isoformat().replace('+00:00','Z'), action='send',
            state='refused', code='dispatch_declined', reason='target does not receive dispatch',
            target={'kind':'puppet','id':str(event.operation.target_id),'incarnation':str(event.operation.incarnation_id)},
            node_id=str(self.node.id), project_ids=[str(self.project.id)])])
        self.assertNotIn('ciphertext', str(body))
        with self.assertRaises(ValueError):
            m.AuditEvent.objects.filter(pk=event.pk).update(reason='changed')
        value = self.api().read(self.actor).results[0]
        with self.assertRaises((AttributeError, TypeError)):
            value.reason = 'changed'

    def test_project_filter_is_intersection_not_authority_and_no_shared_node_leak(self):
        visible = self.event()
        self.event(projects=[self.other])
        self.event(projects=[self.project,self.other])
        self.event(projects=[self.project], action='drain')
        self.assertEqual([row['id'] for row in self.page()['results']], [str(visible.pk)])
        self.assertEqual([row['id'] for row in self.page({'project_id':str(self.project.id)})['results']], [str(visible.pk)])
        self.assertEqual(self.get({'project_id':str(self.other.id)}).status_code, 404)
        self.assertEqual(self.get({'project_id':str(uuid4())}).status_code, 404)

    def test_fully_authorized_multi_project_rows_keep_complete_scope(self):
        a.ProjectMembership.objects.create(user=self.user, source=self.source, project=self.other)
        event = self.event(projects=[self.project,self.other], action='drain')
        row = self.page({'project_id':str(self.project.pk)})['results'][0]
        self.assertEqual(row['project_ids'], sorted([str(self.project.pk),str(self.other.pk)]))
        self.assertEqual(row['target'], {'kind':'node','id':str(event.operation.node_id),'incarnation':None})

    def test_invisible_new_rows_are_filtered_before_limit_not_starving_visible_ones(self):
        visible = self.event()
        for _ in range(105):
            self.event(projects=[self.other])
        body = self.page({'limit':'1'})
        self.assertEqual([row['id'] for row in body['results']], [str(visible.pk)])
        self.assertIsNone(body['next_cursor'])

    def test_node_service_cannot_read_another_node_even_in_the_same_project(self):
        key = auth.enroll_service('audit-node', self.node.id, project_ids=[self.project.id], permissions=['execution.poll_work'])
        user = get_user_model().objects.get(auth_token__key=key)
        user.user_permissions.add(Permission.objects.get(content_type__app_label='operations', codename='view_audit'))
        own = self.event()
        self.event(node=self.other_node)
        self.event(projects=[self.other])
        for _ in range(3):
            self.event(node=self.other_node)
        body = self.page({'limit':'1'}, client=Client(HTTP_AUTHORIZATION='Token '+key))
        self.assertEqual([row['id'] for row in body['results']], [str(own.pk)])
        a.ServiceEnrollment.objects.filter(actor=user).delete()
        self.assertIn(self.get(client=Client(HTTP_AUTHORIZATION='Token '+key)).status_code, (401,403))

    def test_node_service_does_not_infer_multinode_build_scope_from_storage_anchor(self):
        key = auth.enroll_service('audit-build-node', self.node.id, project_ids=[self.project.id], permissions=['execution.poll_work'])
        user = get_user_model().objects.get(auth_token__key=key)
        user.user_permissions.add(Permission.objects.get(content_type__app_label='operations', codename='view_audit'))
        self.event(action='build')
        self.assertEqual(self.page(client=Client(HTTP_AUTHORIZATION='Token '+key))['results'], [])

    def test_no_scope_missing_permission_and_actor_kind_are_not_global_privilege(self):
        self.event()
        for kind in ('human','agent'):
            user, token = self.reader('kind-'+kind, kind=kind)
            response = self.get(client=Client(HTTP_AUTHORIZATION='Token '+token))
            self.assertEqual(response.status_code, 403, response.content)
        user, token = self.reader('staff', self.project, permission=False)
        get_user_model().objects.filter(pk=user.pk).update(is_staff=True)
        self.assertEqual(self.get(client=Client(HTTP_AUTHORIZATION='Token '+token)).status_code, 403)
        self.assertEqual(self.get(client=Client()).status_code, 401)
        self.event(projects=[])
        self.assertEqual(len(self.page()['results']), 1, 'missing operation scope is not public')

    def test_explicit_all_membership_and_superuser_use_current_project_scope(self):
        self.event(projects=[self.other])
        a.AllProjectMembership.objects.create(user=self.user, source=self.source)
        self.assertEqual(len(self.page()['results']), 1)
        a.AllProjectMembership.objects.filter(user=self.user).delete()
        get_user_model().objects.filter(pk=self.user.pk).update(is_superuser=True)
        self.assertEqual(len(self.page()['results']), 1)

    def test_keyset_uses_timestamp_and_id_ties_and_excludes_newer_concurrent_inserts(self):
        at = timezone.now()-timedelta(minutes=1)
        events = [self.event(at=at) for _ in range(5)]
        first = self.page({'limit':'2'})
        self.assertEqual([row['id'] for row in first['results']], [str(row.pk) for row in events[::-1][:2]])
        def commit():
            close_old_connections()
            try:
                return self.event().pk
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=1) as executor:
            newer = executor.submit(commit).result(timeout=10)
        second = self.page({'limit':'2','cursor':first['next_cursor']})
        third = self.page({'limit':'2','cursor':second['next_cursor']})
        ids = [row['id'] for body in (first,second,third) for row in body['results']]
        self.assertEqual(ids, [str(row.pk) for row in events[::-1]])
        self.assertNotIn(str(newer), ids)
        self.assertIsNone(third['next_cursor'])
        self.assertIn(str(newer), [row['id'] for row in self.page()['results']])

    def cursor(self):
        self.event()
        self.event()
        return self.page({'limit':'1'})['next_cursor']

    def test_cursors_are_bound_to_credential_filter_limit_epoch_and_expiry(self):
        from django.core import signing
        token = self.cursor()
        other_user, other_token = self.reader('same-scope', self.project)
        for query, client in (({'cursor':token+'bad','limit':'1'}, None),
                ({'cursor':token,'limit':'2'}, None),
                ({'cursor':token,'limit':'1','project_id':str(self.project.id)}, None),
                ({'cursor':token,'limit':'1'}, Client(HTTP_AUTHORIZATION='Token '+other_token))):
            response = self.get(query, client=client)
            self.assertEqual(response.status_code, 410, response.content)
            self.assertEqual(response.json()['error']['code'], 'reset_required')
            self.assertEqual(response.json()['reset'], {'required':True})
        with patch.object(signing.time, 'time', return_value=timezone.now().timestamp()+301):
            self.assertEqual(self.get({'cursor':token,'limit':'1'}).status_code, 410)
        from mop.server.controller.models import Installation
        Installation.objects.update(epoch=uuid4())
        self.assertEqual(self.get({'cursor':token,'limit':'1'}).status_code, 410)

    def test_live_permission_membership_token_and_identity_revocation_refuse(self):
        token = self.cursor()
        a.ProjectMembership.objects.create(user=self.user, source=self.source, project=self.other)
        self.assertEqual(self.get({'cursor':token,'limit':'1'}).status_code, 410)
        permission = Permission.objects.get(content_type__app_label='operations', codename='view_audit')
        self.user.user_permissions.remove(permission)
        self.assertEqual(self.get().status_code, 403)
        group = Group.objects.create(name='auditors')
        group.permissions.add(permission)
        self.user.groups.add(group)
        self.assertEqual(self.get().status_code, 200)
        group.permissions.remove(permission)
        self.assertEqual(self.get().status_code, 403)
        self.user.user_permissions.add(permission)
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertIn(self.get().status_code, (401,403))
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=True)
        auth.revoke_token(self.actor)
        self.assertIn(self.get().status_code, (401,403))
        with self.assertRaises(auth.Denied):
            self.api().read(self.actor)

    def test_permission_removed_on_independent_connection_during_select_never_returns_data(self):
        self.event()
        def revoke():
            close_old_connections()
            try:
                user = get_user_model().objects.get(pk=self.user.pk)
                user.user_permissions.clear()
            finally:
                close_old_connections()
        changed = False
        def intercept(execute, sql, params, many, context):
            nonlocal changed
            result = execute(sql, params, many, context)
            if not changed and sql.lstrip().startswith('SELECT') and '"operations_auditevent"' in sql:
                changed = True
                with ThreadPoolExecutor(max_workers=1) as executor:
                    executor.submit(revoke).result(timeout=10)
            return result
        with connection.execute_wrapper(intercept):
            response = self.get()
        self.assertTrue(changed, 'exercise actual audit SQL, not a mocked list')
        self.assertEqual(response.status_code, 403, response.content)
        self.assertNotIn('results', response.json())

    def test_node_scope_added_during_select_never_discloses_stale_node_evidence(self):
        # HYPOTHESIS: project proof один не замечает новый чужой NodeProject.
        NodeProject.objects.filter(node=self.node, project=self.other).delete()
        self.event(action='drain')
        changed = False
        def intercept(execute, sql, params, many, context):
            nonlocal changed
            result = execute(sql, params, many, context)
            if not changed and sql.lstrip().startswith('SELECT') and '"operations_auditevent"' in sql:
                changed = True
                NodeProject.objects.create(node=self.node, project=self.other)
            return result
        with connection.execute_wrapper(intercept):
            response = self.get()
        self.assertTrue(changed)
        self.assertEqual(response.status_code, 410, response.content)
        self.assertNotIn('results', response.json())

    def test_current_epoch_restore_guard_rechecked_during_select(self):
        self.event()
        from mop.server.controller.models import Installation, RestoreGuard
        changed = False
        def intercept(execute, sql, params, many, context):
            nonlocal changed
            result = execute(sql, params, many, context)
            if not changed and sql.lstrip().startswith('SELECT') and '"operations_auditevent"' in sql:
                changed = True
                Installation.objects.update(epoch=uuid4())
            return result
        with connection.execute_wrapper(intercept):
            self.assertEqual(self.get().status_code, 410)
        RestoreGuard.objects.update(state='restoring')
        response = self.get()
        self.assertEqual(response.status_code, 503, response.content)
        self.assertNotIn('results', response.json())

    def test_get_is_readonly_default_limit_is_100_and_unknown_inputs_decline(self):
        for _ in range(102):
            self.event()
        statements = []
        def record(execute, sql, params, many, context):
            statements.append(sql)
            return execute(sql, params, many, context)
        with connection.execute_wrapper(record):
            page = self.page()
        self.assertEqual(len(page['results']), 100)
        self.assertTrue(page['next_cursor'])
        self.assertFalse(any(sql.lstrip().split()[0] in ('INSERT','UPDATE','DELETE') for sql in statements), statements)
        for query in ('limit=0','limit=101','limit=-1','limit=1.0','limit=true','limit=01','limit=1&limit=2',
                      'project_id=bad','project_id='+str(self.project.pk).upper(), 'limit=1&unknown=true','cursor='):
            response = self.client.get('/api/v1/audit?'+query, secure=True)
            self.assertIn(response.status_code, (400,410), (query,response.content))
            self.assertIn('no-store', response.headers.get('Cache-Control', ''))
        for method in ('post','put','patch','delete'):
            response = getattr(self.client, method)('/api/v1/audit', secure=True)
            self.assertEqual(response.status_code, 405)
            self.assertIn('no-store', response.headers.get('Cache-Control', ''))
        response = self.get(client=Client())
        self.assertEqual(response.status_code, 401)
        self.assertIn('no-store', response.headers.get('Cache-Control', ''))

    def test_bounded_scope_requires_explicit_project_instead_of_silent_empty_success(self):
        extra = Project.objects.bulk_create([Project(name='scope-'+str(i), origin='https://example.invalid/scope-'+str(i)+'.git') for i in range(101)])
        a.ProjectMembership.objects.bulk_create([a.ProjectMembership(user=self.user, source=self.source, project=row) for row in extra])
        self.event()
        self.assertEqual(self.get().status_code, 503)
        self.assertEqual(len(self.page({'project_id':str(self.project.pk)})['results']), 1)

    def test_openapi_finite_audit_contract_is_generated_from_the_real_route(self):
        from mop.server.controller.openapi import schema
        document = schema()
        self.assertIn('/api/v1/audit', document['paths'])
        route = document['paths']['/api/v1/audit']
        self.assertEqual(set(route), {'get'})
        self.assertEqual({row['name'] for row in route['get']['parameters']}, {'project_id','cursor','limit'})
        name = route['get']['responses']['200']['content']['application/json']['schema']['$ref'].split('/')[-1]
        reply = document['components']['schemas'][name]
        self.assertEqual(set(reply['properties']), {'schema_version','epoch','results','next_cursor','coverage'})
        item_name = reply['properties']['results']['items']['$ref'].split('/')[-1]
        item = document['components']['schemas'][item_name]
        self.assertEqual(set(item['properties']), {'id','operation_id','revision','created_at','action','state','code','reason','target','node_id','project_ids'})
        self.assertEqual(item['properties']['revision']['type'], 'string')


import _delegation_cases as grant_fixture


@override_settings(ALLOWED_HOSTS=['testserver'])
class DelegatedAudit(TransactionTestCase):
    serialized_rollback = True
    setUp = grant_fixture.DelegationCases.setUp
    permit = grant_fixture.DelegationCases.permit
    publish = grant_fixture.DelegationCases.publish
    register_subject = grant_fixture.DelegationCases.register_subject
    reserve = grant_fixture.DelegationCases.reserve
    delegate = grant_fixture.DelegationCases.delegate

    def test_actual_grant_bounds_target_incarnation_owner_permission_and_revocation(self):
        from mop.server.controller.runtime import installation_snapshot
        self.epoch = UUID(installation_snapshot()['epoch'])
        self.node = AuditQueries.make_node(self, 'delegated')
        NodeProject.objects.create(node=self.node, project=self.project)
        self.permit('operations.view_audit')
        target = uuid4()
        _, agent, key = self.register_subject(target_id=target, permissions=('operations.view_audit',))
        grant = self.delegate(target_id=target)
        event = AuditQueries.event(self)
        m.Operation.objects.filter(pk=event.operation_id).update(target_id=target, incarnation_id=grant.binding.incarnation)
        AuditQueries.event(self)
        actor = auth.token_context(key, grants=self.api.local_authority())
        from mop.server.operations.audit import AuditQueries as Query
        queries = Query(epoch=lambda:self.epoch, grants=self.api.local_authority())
        self.assertEqual([row.id for row in queries.read(actor).results], [str(event.pk)])
        permission = Permission.objects.get(content_type__app_label='operations', codename='view_audit')
        self.user.user_permissions.remove(permission)
        with self.assertRaises(auth.Denied):
            queries.read(actor)
        self.user.user_permissions.add(permission)
        self.api.revoke(self.actor, grant.id, expected_revision=grant.revision)
        with self.assertRaises(auth.Denied):
            queries.read(actor)
