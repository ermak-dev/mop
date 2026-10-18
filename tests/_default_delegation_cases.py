"""Production default roots делегирования #715 на настоящем PostgreSQL.

HYPOTHESIS: HTTP и subject service используют разные либо unbound grant roots;
ручной Grants без Preparation теряет exact approval даже при действующем RBAC.
Fixture вызывает реальные publish/approve/reserve/provision/activate, CurrentBindings
читает настоящие SQL Assignment и identity/readiness receipts. Receipts засеиваются:
это проверка потребления production authority, не запуск executor/пула.
Ни grant_resolver, ни production service roots не подменяются.
RED: PG17 — snapshot/delivery 403 delegation resolver is not bound;
operation/stop/delete/worker — approved policy query is not bound;
restart HTTP — 403 вместо отказа несвязанного physical порта.
SOLUTION: один local_grants root с Preparation.approval_query; PoolQueries
получает явный grants port и общий ReadScope до LIMIT, proof перечитывается.
RESULT: PG17 — 176 проверок прошли, включая 14 production-default regressions;
слои controller/common — 2/2, отзыв и несвязанные physical порты закрыты.
STATUS: FIXED — see #715
"""
from datetime import timedelta
import json
from unittest.mock import patch
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from _grant_cases import delegation_subject, manifest, permit_actions


@override_settings(ALLOWED_HOSTS=['testserver'])
class DefaultDelegationRoots(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        from mop.server.access import models as a, services as auth
        from mop.server.access.grants import Grants
        from mop.server.catalog.models import Project, Node, NodeProject, ClientRegistration
        from mop.server.controller.credentials import CurrentBindings
        from mop.server.controller.models import Installation, RestoreGuard
        from mop.server.controller.runtime import initialize
        from mop.server.preparation.services import Preparation
        from mop.server.secrets.crypto import Keyring, XChaCha20Provider
        self.epoch = UUID(initialize()['epoch'])
        Installation.objects.update(effects_enabled=True)
        self.generation = RestoreGuard.objects.get(pk=1).generation
        self.auth, self.a = auth, a
        self.project = Project.objects.create(name='allowed', origin='https://example.invalid/allowed.git')
        self.foreign = Project.objects.create(name='foreign', origin='https://example.invalid/foreign.git')
        self.node = Node.objects.create(name='allowed', driver='host', ssh_host='node.invalid',
            control_origin='https://node.invalid', lan_address='node.invalid', bootstrap_address='node.invalid')
        self.other_node = Node.objects.create(name='foreign', driver='host', ssh_host='other.invalid',
            control_origin='https://other.invalid', lan_address='other.invalid', bootstrap_address='other.invalid')
        for node, project in ((self.node, self.project), (self.other_node, self.project), (self.node, self.foreign)):
            NodeProject.objects.create(node=node, project=project)
        self.source = a.IdentitySource.objects.create(name='local', kind='local')
        self.owner = get_user_model().objects.create_user('owner')
        a.LoginIdentity.objects.create(user=self.owner, source=self.source, external_subject='owner')
        a.ProjectMembership.objects.create(user=self.owner, project=self.project, source=self.source)
        self.actions = ('catalog.view_pool', 'catalog.view_project', 'execution.send_message',
            'execution.view_delivery', 'access.issue_grant', 'execution.create_puppet')
        permit_actions(self.owner, *self.actions, 'access.view_role', 'access.approve_role',
            'access.revoke_grant', 'preparation.approve_bootstrap', 'secrets.manage_project_secret',
            'operations.view_operation', 'execution.register_endpoint', 'execution.deliver_report')
        self.owner_key = auth.issue_token(self.owner.pk)
        self.human = auth.token_context(self.owner_key)
        self.provider = XChaCha20Provider(Keyring('fixture', {'fixture': b'd'*32}))
        self.preparation = Preparation(provider=self.provider, authorizer=auth.Access())
        revision = self.preparation.publish(self.human, project_id=self.project.pk,
            tasks=b'- debug: msg=hello\n', variables=b'answer: 42\n',
            provenance='git:fixture', commit='a'*40)
        self.approval = self.preparation.approve(self.human, revision.id)
        self.grants = Grants(provider=self.provider, approvals=self.preparation.approval_query(),
                             bindings=CurrentBindings())
        self.definition = self.grants.publish_definition(self.human, project_id=self.project.pk,
            manifest=json.dumps(manifest(spawn=('worker',))).encode(), prompt=b'fixture role', provenance='git:fixture')
        self.grants.approve_definition(self.human, self.definition.id)
        self.executor_key = auth.enroll_service('executor', self.node.pk, project_ids=(self.project.pk,),
            permissions=('execution.poll_work',))
        self.executor = auth.token_context(self.executor_key)
        self.parent, self.issuer, self.parent_key, self.parent_assignment = self.active(self.human)
        self.issuer_actor = self.grants.local_authority().token_context(self.parent_key)
        self.child, self.subject, self.key, self.assignment = self.active(self.issuer_actor)
        self.actor = self.grants.local_authority().token_context(self.key)
        self.client = Client(HTTP_AUTHORIZATION='Token '+self.key)
        self.owner_client = Client(HTTP_AUTHORIZATION='Token '+self.owner_key)
        client = ClientRegistration.objects.create(installation=Installation.objects.get(pk=1), label='fixture')
        a.ClientOwnership.objects.create(user=self.owner, client=client)
        from mop.server.controller.endpointviews import endpoints
        self.endpoint = endpoints.register(self.human, client_id=client.pk, project_id=self.project.pk,
            external_session_id='fixture', display_name='fixture')
        self.delivery = self.delivery_row(self.endpoint.id)
        self.operation = self.operation_row()

    def active(self, issuer):
        from mop.server.sessions import models as m
        profile, subject, key = delegation_subject(self.grants, self.human, project_id=self.project.pk,
            definition_id=self.definition.id, actions=self.actions)
        reserved = self.grants.reserve(issuer, project_id=self.project.pk, definition_id=self.definition.id,
            target_id=profile.target_id, incarnation=uuid4(), request_id=uuid4(),
            expires_at=timezone.now()+timedelta(hours=1))
        assignment = m.Assignment.objects.create(id=reserved.target_id, project=self.project, node=self.node,
            owner=self.owner, role=reserved.role, bundle_digest=reserved.digest, grant_id=reserved.id,
            expires_at=timezone.now()+timedelta(hours=1))
        incarnation = m.ExecutionIncarnation.objects.create(id=reserved.incarnation, assignment=assignment,
            node=self.node, executor_epoch=uuid4())
        assignment.current_incarnation = incarnation
        assignment.save(update_fields=['current_incarnation'])
        provisioned = self.grants.provision(issuer, reserved.id, expected_revision=reserved.revision)
        for model, action, evidence in ((m.IdentityApplication, 'apply_identity', 'identity-applied'),
                                        (m.ReadinessReceipt, 'confirm_ready', 'ready')):
            attempt = m.AssignmentAttempt.objects.create(assignment=assignment, incarnation=incarnation,
                executor_user_id=self.executor.user_id, request_id=uuid4(), epoch=self.epoch,
                operation_id=uuid4(), step_id=uuid4(), executor_epoch=incarnation.executor_epoch,
                owner=self.owner, generation=assignment.generation, assignment_revision=assignment.revision,
                role=reserved.role, bundle_digest=reserved.digest, grant_id=reserved.id, action=action,
                not_after=timezone.now()+timedelta(minutes=5), lease_until=timezone.now()+timedelta(minutes=1),
                state='succeeded', revision=2)
            receipt = m.ResultReceipt.objects.create(attempt=attempt, expected_revision=1,
                resulting_revision=2, outcome='succeeded', evidence=evidence)
            model.objects.create(attempt=attempt, receipt=receipt)
        active = self.grants.activate(reserved.id, expected_revision=provisioned.revision)
        self.assertEqual(active.state, 'active')
        return active, subject, key, assignment

    def operation_row(self, *, actor=None, project=None):
        from mop.server.operations.models import Operation
        from mop.server.operations.service import _binding
        actor = actor or self.actor
        row = Operation.objects.create(actor_id=actor.user_id, actor_binding=_binding(actor), request_id=uuid4(),
            digest='a'*64, epoch=self.epoch, restore_generation=self.generation, action='restart', node=self.node,
            expected_revision=1, target_kind='puppet', target_id=self.assignment.id,
            incarnation_id=self.assignment.current_incarnation_id, not_after=timezone.now()+timedelta(minutes=2))
        row.projects.add(project or self.project)
        return row

    def delivery_row(self, endpoint_id):
        from mop.server.sessions.endpoint_models import MessageDelivery
        return MessageDelivery.objects.create(endpoint_id=endpoint_id, epoch=self.epoch, message_id=uuid4(),
            sender_user_id=self.actor.user_id, sender_credential_id=self.actor.credential_id,
            sender_kind=self.actor.kind.value, sender_owner_id=self.actor.human_owner_id,
            sender_grant_id=self.actor.grant_id, fingerprint='a'*64, priority='next',
            not_after=timezone.now()+timedelta(minutes=2), state='unavailable')

    def get(self, path, params=None):
        return self.client.get('/api/v1/'+path, params or {}, secure=True)

    def assert_ok(self, path, params=None):
        response = self.get(path, params)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn('no-store', {part.strip() for part in response['Cache-Control'].split(',')})
        return response.json()

    def assert_denied(self, path, params=None):
        response = self.get(path, params)
        self.assertIn(response.status_code, (403, 404), response.content)
        return response

    def remove_permission(self, user, action):
        app, code = action.split('.')
        user.user_permissions.remove(Permission.objects.get(content_type__app_label=app, codename=code))

    def test_default_snapshot_and_detail_use_real_exact_approval_without_provider_io(self):
        with patch.object(type(self.provider), 'encrypt', side_effect=AssertionError('provider in read root')), \
             patch.object(type(self.provider), 'decrypt', side_effect=AssertionError('provider in read root')):
            snapshot = self.assert_ok('snapshot')
            self.assertEqual([row['id'] for row in snapshot['results']], [str(self.assignment.id)])
            self.assertEqual(self.assert_ok('puppets/'+str(self.assignment.id))['puppet']['id'], str(self.assignment.id))
            self.assertEqual(self.assert_ok('puppets')['results'], snapshot['results'])

    def test_default_operation_status_and_request_lookup_use_common_approval_root(self):
        with patch.object(type(self.provider), 'decrypt', side_effect=AssertionError('provider in operation read')):
            self.assertEqual(self.assert_ok('operations/'+str(self.operation.id))['operation']['id'], str(self.operation.id))
            value = self.assert_ok('operations', {'request_id':str(self.operation.request_id), 'epoch':str(self.epoch)})
            self.assertEqual(value['operation']['id'], str(self.operation.id))

    def test_default_delivery_metadata_is_allowed_but_human_endpoint_metadata_is_not(self):
        with patch.object(type(self.provider), 'decrypt', side_effect=AssertionError('provider in metadata read')):
            self.assertEqual(self.assert_ok('deliveries/'+str(self.delivery.id))['delivery']['id'], str(self.delivery.id))
        self.assert_denied('addresses', {'schema_version':'1', 'project_id':str(self.project.pk)})
        self.assert_denied('addresses/'+str(self.endpoint.id))

    def test_foreign_project_target_and_node_are_not_disclosed(self):
        from mop.server.sessions.models import Assignment, ExecutionIncarnation
        for project, node in ((self.project, self.other_node), (self.foreign, self.node)):
            row = Assignment.objects.create(project=project, node=node, owner=self.owner, role='worker',
                bundle_digest='b'*64, grant_id=uuid4(), expires_at=timezone.now()+timedelta(hours=1))
            incarnation = ExecutionIncarnation.objects.create(assignment=row, node=node, executor_epoch=uuid4())
            row.current_incarnation = incarnation
            row.save(update_fields=['current_incarnation'])
            self.assert_denied('puppets/'+str(row.pk))
        self.assert_denied('puppets/'+str(self.parent_assignment.id))
        self.assert_denied('snapshot', {'project_id':str(self.foreign.pk)})
        self.assertEqual([row['id'] for row in self.assert_ok('snapshot')['results']], [str(self.assignment.pk)])
        other = self.operation_row(actor=self.issuer_actor)
        self.assert_denied('operations/'+str(other.pk))
        self.assert_denied('operations', {'request_id':str(other.request_id), 'epoch':str(self.epoch)})
        from mop.server.sessions.endpoint_models import Endpoint
        foreign_endpoint = Endpoint.objects.create(session_id=self.endpoint.session_id, project=self.foreign,
            expires_at=timezone.now()+timedelta(hours=1))
        foreign_delivery = self.delivery_row(foreign_endpoint.id)
        self.assert_denied('deliveries/'+str(foreign_delivery.id))

    def test_owner_subject_and_issuer_permission_revocation_after_read(self):
        for user in (self.owner, self.subject, self.issuer):
            with self.subTest(user=user.pk):
                self.assert_ok('snapshot')
                self.remove_permission(user, 'catalog.view_pool')
                self.assert_denied('snapshot')
                self.assert_denied('puppets/'+str(self.assignment.pk))
                permit_actions(user, 'catalog.view_pool')
                self.assert_ok('deliveries/'+str(self.delivery.id))
                self.remove_permission(user, 'execution.send_message')
                self.assert_denied('deliveries/'+str(self.delivery.id))
                permit_actions(user, 'execution.send_message')
        self.assert_ok('operations/'+str(self.operation.pk))
        self.remove_permission(self.owner, 'operations.view_operation')
        self.assert_denied('operations/'+str(self.operation.pk))

    def test_ancestor_credential_revocation_closes_every_default_read_after_success(self):
        paths = ('snapshot', 'puppets/'+str(self.assignment.pk), 'operations/'+str(self.operation.pk),
                 'deliveries/'+str(self.delivery.pk))
        for path in paths:
            self.assert_ok(path)
        self.a.CredentialBinding.objects.filter(token_user=self.issuer).delete()
        for path in paths:
            self.assert_denied(path)

    def test_owner_subject_and_issuer_identity_revocation_closes_each_default_read(self):
        paths = ('snapshot', 'puppets/'+str(self.assignment.pk), 'operations/'+str(self.operation.pk),
                 'deliveries/'+str(self.delivery.pk))
        for user in (self.owner, self.subject, self.issuer):
            with self.subTest(user=user.pk):
                for path in paths:
                    self.assert_ok(path)
                get_user_model().objects.filter(pk=user.pk).update(is_active=False)
                for path in paths:
                    response = self.get(path)
                    self.assertIn(response.status_code, (401, 403, 404), response.content)
                get_user_model().objects.filter(pk=user.pk).update(is_active=True)

    def test_exact_preparation_approval_revocation_closes_every_default_read(self):
        self.assert_ok('snapshot')
        self.preparation.revoke(self.human, self.approval.id)
        for path in ('snapshot', 'puppets/'+str(self.assignment.pk), 'operations/'+str(self.operation.pk),
                     'deliveries/'+str(self.delivery.pk)):
            self.assert_denied(path)

    def test_snapshot_rechecks_live_permissions_after_fact_select(self):
        fired = []
        def revoke(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if not fired and sql.lstrip().startswith('SELECT') and '"execution_assignment"' in sql and 'source_state' in sql:
                fired.append(True)
                self.remove_permission(self.issuer, 'catalog.view_pool')
            return result
        with connection.execute_wrapper(revoke):
            response = self.get('snapshot')
        self.assertEqual(fired, [True], 'must reach the real projection SELECT')
        self.assertIn(response.status_code, (403, 409), response.content)

    def test_event_cursor_is_target_scoped_and_revocation_cannot_reuse_it(self):
        from mop.server.sessions.projection_writes import ProjectionWriter
        run = ProjectionWriter.begin_run(self.epoch)
        writer = ProjectionWriter(run)
        cursor = self.assert_ok('snapshot')['cursor']
        with transaction.atomic():
            writer.changed(target_id=self.parent_assignment.id)
            writer.changed(target_id=self.assignment.id)
        result = self.assert_ok('events', {'cursor':cursor})
        self.assertEqual([row['entity']['id'] for row in result['results']], [str(self.assignment.pk)])
        self.remove_permission(self.subject, 'catalog.view_pool')
        self.assert_denied('events', {'cursor':result['cursor']})

    def test_pruned_operation_results_stay_expired(self):
        from mop.server.operations.models import Operation
        self.assert_ok('operations/'+str(self.operation.id))
        Operation.objects.filter(pk=self.operation.id).update(result_pruned=True, completed_at=timezone.now(), state='succeeded')
        response = self.get('operations/'+str(self.operation.id))
        self.assertEqual(response.status_code, 410, response.content)

    def test_default_command_and_worker_roots_resolve_the_same_live_chain(self):
        from mop.server.controller import operationviews as views
        from mop.server.controller.runtime import Runtime, compose_worker
        class NodePort:
            async def set_eligibility(inner, *args):
                raise AssertionError('composition must not execute physical effects')
        for factory in (views.operations_api, views.restart_api, views.stop_api, views.delete_api,
                        views.create_api, views.update_api, views.wipe_api):
            with self.subTest(root=factory.__name__):
                self.assertEqual(factory().grants.token_context(self.key), self.actor)
        worker = compose_worker(Runtime(), node_executor=NodePort(), executor=self.executor)
        self.assertEqual(worker.operations.grants.token_context(self.key), self.actor)
        for view in (views.DrainNode, views.RestartPuppet, views.StopPuppetJob, views.DeletePuppet,
                     views.CreatePuppet, views.UpdatePuppet, views.WipePuppet):
            with self.subTest(view=view.__name__):
                self.assertEqual(self.auth.token_context(self.key,
                    grants=getattr(view(), 'grant_resolver', None)), self.actor)

    def test_default_restart_reaches_unbound_physical_gate_not_missing_resolver(self):
        body = {'schema_version':1, 'epoch':str(self.epoch), 'request_id':str(uuid4()),
            'incarnation':str(self.assignment.current_incarnation_id),
            'expected_revision':str(self.assignment.revision),
            'not_after':(timezone.now()+timedelta(minutes=1)).isoformat(), 'params':{'fresh':False}}
        response = self.client.post('/api/v1/puppets/'+str(self.assignment.id)+'/commands/restart',
            body, content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(response.json()['error']['code'], 'controller_unavailable')

    def test_unbound_services_and_physical_cipher_still_deny(self):
        from mop.server.access.services import Access, current_actor
        from mop.server.sessions.projections import PoolQueries
        from mop.server.sessions.endpoints import Endpoints, Unavailable
        from mop.server.controller.endpointviews import endpoints
        from mop.server.controller.operationviews import restart_api
        from mop.server.operations.commands import RestartCommand, TargetIntent
        with self.assertRaises(PermissionError):
            current_actor(self.actor)
        with self.assertRaises(PermissionError):
            Access().authorize(self.actor, 'catalog.view_pool', project_ids=(self.project.pk,))
        with self.assertRaises(PermissionError):
            PoolQueries(epoch=lambda:self.epoch).snapshot(self.actor)
        with self.assertRaisesRegex(Unavailable, 'endpoint authority is not bound'):
            Endpoints().delivery(self.actor, self.delivery.id)
        # Production cipher не связан; SQL metadata не превращается в private send.
        endpoints.connect(self.human, self.endpoint.id, gateway_id=uuid4())
        with self.assertRaisesRegex(Unavailable, 'private delivery cipher is not bound'):
            endpoints.send(self.actor, expected_epoch=self.epoch, address=self.endpoint.address, message_id=uuid4(),
                text='not sent', not_after=timezone.now()+timedelta(minutes=1))
        command = RestartCommand(TargetIntent(self.epoch, uuid4(), self.assignment.id,
            self.assignment.current_incarnation_id, self.assignment.revision, timezone.now()+timedelta(minutes=1)), False)
        with self.assertRaises((PermissionError, ValueError)):
            restart_api().submit(self.actor, command)
