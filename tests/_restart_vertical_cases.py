"""Сквозной restart #625/#639: настоящие permissions, Operation/Step и grants."""
from datetime import timedelta
from uuid import UUID, uuid4
import json

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.test import TransactionTestCase, Client, override_settings
from django.utils import timezone

from mop.server.access import services as auth, models as a
from mop.server.catalog.models import Project, Node, NodeProject
from mop.server.controller.runtime import initialize
from mop.server.controller.models import Installation, RestoreGuard

# FEATURE: существующий durable Step проецируется в machine HTTP без второй очереди.
# HYPOTHESIS: node-only Operation не может принять restart; логические sessions
# permissions отсутствуют на настоящих model content types новой БД.
# SPEC: независимый COMMIT, актуальные actor/grant/scope, unknown сохраняет fence.
# RED: PG16 — отсутствовали настоящие permissions и restart route (404);
#      PG16/17 — input/epoch endpoints, overlap с drain, commit/outbox/retirement,
#      shared-node scope, replay после retirement и ложный catalog receipt.
# SOLUTION: typed stages общего журнала, публичные SQL ports в controller root,
#           actual role binding, independent COMMIT и fixed private input.
# RESULT: PG16/17 — по 148 combined checks; native TLS/tmux/Unix socket proof.
# STATUS: FIXED — see #625
@override_settings(ALLOWED_HOSTS=['testserver'])
class RestartVertical(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        RestoreGuard.objects.get_or_create(pk=1)
        initialize()
        Installation.objects.update(effects_enabled=True)
        self.epoch = Installation.objects.get().epoch
        self.project = Project.objects.create(name='vertical', origin='https://example.invalid/vertical.git')
        self.node = Node.objects.create(name='fixture', driver='host', ssh_host='example.invalid',
            control_origin='https://example.invalid', lan_address='example.invalid', bootstrap_address='example.invalid')
        NodeProject.objects.create(node=self.node, project=self.project)
        self.user = get_user_model().objects.create_superuser('owner', email='owner@example.invalid', password='fixture-only')
        self.user.first_name, self.user.last_name = 'Fixture', 'Owner'
        self.user.save()
        source = a.IdentitySource.objects.create(name='local', kind='local')
        a.LoginIdentity.objects.create(user=self.user, source=source, external_subject='owner')
        self.key = auth.issue_token(self.user.pk)
        self.actor = auth.token_context(self.key)
        self.client = Client(HTTP_AUTHORIZATION='Token '+self.key)

    def fixture(self):
        # Approval — явный внешний порт; grant/credential/role seal настоящие.
        from _grant_cases import ApprovalFixture, BindingFixture, manifest
        from mop.server.access.grants import Grants, ApprovedPolicy, RoleBinding
        from mop.server.secrets.crypto import Keyring, XChaCha20Provider
        from mop.server.secrets.services import SecretStore
        from mop.server.controller.runtime import assignments_service as Assignments
        from mop.server.sessions.models import Assignment
        provider = XChaCha20Provider(Keyring('fixture', {'fixture': b'z'*32}))
        policy = SecretStore(provider=provider, authorizer=auth.Access()).create(self.actor,
            project_id=self.project.pk, name='policy', kind='credential', purpose='policy', value=b'approved fixture policy')
        proof = ApprovedPolicy(uuid4(), policy.active_version_id, 'a'*64, self.user.pk, self.project.pk)
        self.bindings = BindingFixture()
        self.grants = Grants(provider=provider, approvals=ApprovalFixture(proof), bindings=self.bindings)
        raw = json.dumps(manifest(receives=False)).encode()
        bundle = self.grants.publish_definition(self.actor, project_id=self.project.pk, manifest=raw,
            prompt=b'fixture role', provenance='controlled fixture')
        self.grants.approve_definition(self.actor, bundle.id)
        target_id, incarnation_id = uuid4(), uuid4()
        # #688: субъект — заранее зарегистрированный agent Actor со своим token.
        from _grant_cases import delegation_subject
        delegation_subject(self.grants, self.actor, project_id=self.project.pk,
            definition_id=bundle.id, target_id=target_id)
        grant = self.grants.reserve(self.actor, project_id=self.project.pk, definition_id=bundle.id,
            target_id=target_id, incarnation=incarnation_id, request_id=uuid4(), expires_at=timezone.now()+timedelta(hours=1))
        self.grants.provision(self.actor, grant.id, expected_revision=grant.revision)
        binding = RoleBinding(self.project.pk, target_id, incarnation_id, grant.role, grant.digest, grant.id, self.user.pk)
        self.bindings.bindings[(self.project.pk, target_id)] = binding
        self.bindings.receipts[grant.id] = binding
        self.grants.activate(grant.id, expected_revision=self.grants.get(self.actor, grant.id).revision)
        self.boot = uuid4()
        # Тестовая регистрация layout/назначения, не production обход permissions.
        row = Assignment.objects.create(id=target_id, project=self.project, node=self.node,
            owner=self.user, role=grant.role, bundle_digest=grant.digest, grant_id=grant.id,
            expires_at=timezone.now()+timedelta(hours=1))
        Assignments().activate(self.actor, row.id, expected_revision=row.revision,
            executor_epoch=self.boot, incarnation_id=incarnation_id)
        self.target = Assignment.objects.get(pk=target_id)
        self.machine_key = auth.enroll_service('fixture-executor', self.node.pk, project_ids=(self.project.pk,),
            permissions=('execution.poll_work','execution.claim_work','execution.report_result','execution.read_facts'))
        self.machine_actor = auth.token_context(self.machine_key)
        self.machine = Client(HTTP_AUTHORIZATION='Token '+self.machine_key)
        from mop.server.controller.machinebinding import AssignmentBindings, operations
        self.grants.bindings = AssignmentBindings()
        class RegisteredSupport:
            # Явный внешний capability port этого unit fixture, не production default.
            def require(inner, value):
                if (value.target_id, value.incarnation_id) != (target_id, incarnation_id):
                    raise PermissionError('unregistered fixture binding')
        self.support = RegisteredSupport()
        from unittest.mock import patch
        self.addCleanup(patch.stopall)
        patch('mop.server.controller.operationviews.restart_api',
            lambda: operations(grants=self.grants, restart_support=self.support)).start()
        return binding

    def body(self, **changes):
        result = dict(schema_version=1, epoch=str(self.epoch), request_id=str(uuid4()),
            incarnation=str(self.target.current_incarnation_id), expected_revision=str(self.target.revision),
            not_after=(timezone.now()+timedelta(minutes=2)).isoformat(), params={'fresh':False})
        result.update(changes)
        return result

    # HYPOTHESIS: независимые roots restart/build теряют общий target claim.
    # SOLUTION: machine root использует общий SQL target_held через runtime factory.
    # RESULT: PG16 — unbound вместо query; после fix PG17 narrow 4/4 green.
    # STATUS: FIXED — see #623.
    # Проверяем настоящую SQL query, не permissive callback и не снятие default deny.
    def test_root_machine_composition_uses_real_common_target_fence(self):
        from mop.server.controller.machinebinding import compose
        from mop.server.operations.resources import target_held
        self.assertIs(compose().target_fence, target_held)

    def test_permissions_exist_on_actual_domain_model_content_types(self):
        from mop.server.access import permissions
        for action in permissions.ACTIONS:
            for permission in getattr(permissions, 'permission_names', lambda value: (value,))(action):
                app, code = permission.split('.')
                rows = Permission.objects.filter(content_type__app_label=app, codename=code)
                self.assertTrue(rows.exists(), 'unregistered production permission: '+permission)
                for row in rows:
                    self.assertIsNotNone(row.content_type.model_class(), 'fake production content type: '+permission)

    def admit(self):
        self.fixture()
        response = self.client.post('/api/v1/puppets/'+str(self.target.id)+'/commands/restart', self.body(),
            content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 202, response.content)
        from mop.server.controller.machinebinding import compose
        service = compose(grants=self.grants, restart_support=self.support)
        work, = service.poll(self.machine_actor, self.node.id, executor_epoch=self.boot, limit=1)
        return response.json()['operation'], service, work

    def test_executor_discovers_previous_epoch_from_server_and_cas_fences_restart(self):
        self.fixture()
        from mop.server.controller.machinebinding import compose
        from unittest.mock import patch
        service = compose(grants=self.grants, restart_support=self.support)
        with patch('mop.server.controller.machineviews.execution', service):
            response = self.machine.get('/api/v1/machine/executor', {'schema_version':'1','node_id':str(self.node.id)}, secure=True)
            self.assertEqual(response.status_code,200,response.content)
            self.assertEqual(response.json()['executor_epoch'],str(self.boot))
            new = uuid4()
            response = self.machine.post('/api/v1/machine/executor/restarted',dict(schema_version=1,node_id=str(self.node.id),
                previous_epoch=str(uuid4()),new_epoch=str(new)),content_type='application/json',secure=True)
            self.assertEqual(response.status_code,409,response.content)
            response = self.machine.post('/api/v1/machine/executor/restarted',dict(schema_version=1,node_id=str(self.node.id),
                previous_epoch=str(self.boot),new_epoch=str(new)),content_type='application/json',secure=True)
            self.assertEqual(response.status_code,200,response.content)

    def test_action_input_is_addressed_live_owner_profile_not_node_json(self):
        operation, service, work = self.admit()
        attempt = service.claim(self.machine_actor, work, request_id=uuid4())
        from unittest.mock import patch
        with patch('mop.server.controller.machineviews.execution', service):
            response = self.machine.get('/api/v1/machine/work/input', {'schema_version':'1',
                'operation_id':str(work.operation_id), 'step_id':str(work.step_id)}, secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {'schema_version':1, 'operation_id':str(work.operation_id),
            'step_id':str(work.step_id), 'git_name':'Fixture Owner', 'git_email':'owner@example.invalid'})

    def test_unknown_blocks_all_remaining_steps_and_new_restart_until_receipt(self):
        operation, service, work = self.admit()
        attempt = service.claim(self.machine_actor, work, request_id=uuid4())
        service.result(self.machine_actor, attempt.id, expected_revision=1, receipt_id=uuid4(),
            outcome='unknown', evidence='connection-lost')
        self.assertEqual(service.poll(self.machine_actor, self.node.id, executor_epoch=self.boot), ())
        status = self.client.get('/api/v1/operations/'+operation['id'], secure=True).json()['operation']
        self.assertEqual(status['state'], 'unknown')
        self.assertTrue(status['resources_held'])
        self.assertEqual(len(status['steps']), 3)
        response = self.client.post('/api/v1/puppets/'+str(self.target.id)+'/commands/restart',self.body(),
            content_type='application/json',secure=True)
        self.assertEqual(response.status_code,202,response.content)
        next_work, = service.poll(self.machine_actor,self.node.id,executor_epoch=self.boot,limit=1)
        self.assertNotEqual(next_work.operation_id,work.operation_id)
        from mop.server.sessions.services import Conflict
        with self.assertRaisesRegex(Conflict,'unresolved external effect'):
            service.claim(self.machine_actor,next_work,request_id=uuid4())

    def test_restart_and_status_require_target_project_not_unrelated_projects_on_shared_node(self):
        self.fixture()
        foreign = Project.objects.create(name='foreign',origin='https://example.invalid/foreign.git')
        NodeProject.objects.create(node=self.node,project=foreign)
        source = a.LoginIdentity.objects.get(user=self.user).source
        a.ProjectMembership.objects.create(user=self.user,project=self.project,source=source)
        self.user.is_superuser=False; self.user.is_staff=False
        self.user.save(update_fields=['is_superuser','is_staff'])
        for app,code in (('access','view_role'),('execution','restart_puppet'),('operations','view_operation')):
            self.user.user_permissions.add(Permission.objects.get(content_type__app_label=app,codename=code))
        response = self.client.post('/api/v1/puppets/'+str(self.target.id)+'/commands/restart',self.body(),
            content_type='application/json',secure=True)
        self.assertEqual(response.status_code,202,response.content)
        operation = response.json()['operation']
        status = self.client.get('/api/v1/operations/'+operation['id'],secure=True)
        self.assertEqual(status.status_code,200,status.content)
        from mop.server.controller.machinebinding import compose
        service = compose(grants=self.grants,restart_support=self.support)
        work, = service.poll(self.machine_actor,self.node.id,executor_epoch=self.boot,limit=1)
        self.assertEqual(str(work.operation_id),operation['id'])
        service.claim(self.machine_actor,work,request_id=uuid4())

    def test_missing_owner_identity_is_refused_before_operation_admission(self):
        self.fixture()
        self.user.email = ''; self.user.save(update_fields=['email'])
        response = self.client.post('/api/v1/puppets/'+str(self.target.id)+'/commands/restart',self.body(),
            content_type='application/json',secure=True)
        self.assertEqual(response.status_code,503,response.content)
        from mop.server.operations.models import Operation
        self.assertEqual(Operation.objects.count(),0)

    def test_production_default_refuses_missing_physical_binding_before_admission(self):
        self.fixture()
        from mop.server.controller.machinebinding import operations
        from mop.server.operations.commands import parse_restart
        from mop.server.operations.service import Refused
        from mop.server.operations.models import Operation
        with self.assertRaisesRegex(Refused, 'physical restart binding'):
            operations(grants=self.grants).submit(self.actor,parse_restart(self.target.id,self.body()))
        self.assertEqual(Operation.objects.count(),0)

    def test_initiator_revoke_and_receiving_grant_revoke_each_prevent_machine_claim(self):
        operation, service, work = self.admit()
        self.user.is_active = False; self.user.save(update_fields=['is_active'])
        with self.assertRaises(auth.Denied):
            service.claim(self.machine_actor,work,request_id=uuid4())
        self.user.is_active = True; self.user.save(update_fields=['is_active'])
        row = self.grants.get(self.actor,self.target.grant_id)
        self.grants.revoke(self.actor,row.id,expected_revision=row.revision)
        with self.assertRaises(auth.Denied):
            service.claim(self.machine_actor,work,request_id=uuid4())
        from mop.server.sessions.models import AssignmentAttempt
        self.assertEqual(AssignmentAttempt.objects.count(),0)

    def test_common_outbox_exposes_puppet_target_without_private_action_input(self):
        operation, service, work = self.admit()
        from mop.server.controller.runtime import operations_service
        events = []
        self.assertTrue(operations_service().publish_one(events.append))
        event, = events
        self.assertEqual(event['target'],{'kind':'puppet','id':str(self.target.id),
            'incarnation':str(self.target.current_incarnation_id),'revision':str(self.target.revision)})
        self.assertNotIn('Fixture Owner',json.dumps(event))
        self.assertNotIn('owner@example.invalid',json.dumps(event))
        self.assertNotIn(self.machine_key,json.dumps(event))

    def test_actual_pending_projection_scopes_before_bound_and_has_no_second_queue(self):
        operation, service, work = self.admit()
        self.assertEqual(service.work.pending(node_id=self.node.id,executor_epoch=self.boot,project_ids=(),limit=1),())
        self.assertEqual(service.work.pending(node_id=self.node.id,executor_epoch=uuid4(),project_ids=(self.project.id,),limit=1),())
        self.assertEqual(service.work.pending(node_id=self.node.id,executor_epoch=self.boot,project_ids=(self.project.id,),limit=1),(work,))
        from mop.server.sessions.models import AssignmentAttempt
        self.assertEqual(AssignmentAttempt.objects.count(),0)

    def test_retry_conflict_and_duplicate_request_are_one_common_operation(self):
        self.fixture()
        from mop.server.controller.machinebinding import operations
        from mop.server.operations.commands import parse_restart
        from mop.server.operations.service import Refused
        from mop.server.operations.models import Operation
        api = operations(grants=self.grants,restart_support=self.support)
        body = self.body()
        first = api.submit(self.actor,parse_restart(self.target.id,body))
        self.assertEqual(api.submit(self.actor,parse_restart(self.target.id,body))['id'],first['id'])
        changed = {**body,'not_after':(timezone.now()+timedelta(minutes=3)).isoformat()}
        with self.assertRaisesRegex(Refused,'request_conflict'):
            api.submit(self.actor,parse_restart(self.target.id,changed))
        self.assertEqual(Operation.objects.count(),1)

    def test_identical_retry_reads_committed_result_after_executor_binding_is_retired(self):
        self.fixture()
        from mop.server.controller.machinebinding import operations, compose
        from mop.server.operations.commands import parse_restart
        from mop.server.operations.models import Operation
        api = operations(grants=self.grants,restart_support=self.support)
        command = parse_restart(self.target.id,self.body())
        first = api.submit(self.actor,command)
        service = compose(grants=self.grants,restart_support=self.support)
        work, = service.poll(self.machine_actor,self.node.id,executor_epoch=self.boot,limit=1)
        service.claim(self.machine_actor,work,request_id=uuid4())
        service.executor_restarted(self.machine_actor,self.node.id,previous_epoch=self.boot,new_epoch=uuid4())
        result = api.submit(self.actor,command)
        self.assertEqual((result['id'],result['state']),(first['id'],'unknown'))
        self.assertEqual(Operation.objects.count(),1)

    def test_executor_restart_requires_restore_guard_and_independent_commit(self):
        self.fixture()
        from mop.server.controller.machinebinding import compose
        from mop.server.sessions.services import Unavailable
        service = compose(grants=self.grants,restart_support=self.support)
        with transaction.atomic():
            with self.assertRaisesRegex(Unavailable,'surrounding transaction'):
                service.executor_restarted(self.machine_actor,self.node.id,previous_epoch=self.boot,new_epoch=uuid4())
        RestoreGuard.objects.update(state='restoring')
        with self.assertRaisesRegex(Unavailable,'restore guard'):
            service.executor_restarted(self.machine_actor,self.node.id,previous_epoch=self.boot,new_epoch=uuid4())

    def test_executor_restart_after_intermediate_receipt_holds_unknown_workflow(self):
        operation, service, work = self.admit()
        attempt = service.claim(self.machine_actor,work,request_id=uuid4())
        service.result(self.machine_actor,attempt.id,expected_revision=1,receipt_id=uuid4(),
            outcome='succeeded',evidence='identity-applied')
        service.executor_restarted(self.machine_actor,self.node.id,previous_epoch=self.boot,new_epoch=uuid4())
        status = self.client.get('/api/v1/operations/'+operation['id'],secure=True).json()['operation']
        self.assertEqual(status['state'],'unknown')
        self.assertTrue(status['resources_held'])

    # HYPOTHESIS: manual autocommit=False не виден через in_atomic_block;
    # expiry callback впервые берёт guard уже после Assignment, меняя lock order.
    # RED: PG16 — claim вернулся без COMMIT, facts/inspection ports вызваны;
    #      PG17 — первый настоящий FOR UPDATE был Assignment, clone port вызван.
    # SOLUTION: явный autocommit gate и installation guard до domain locks.
    # RESULT: PostgreSQL guards green; внешние порты не вызваны под caller SQL.
    # STATUS: FIXED — see #625/#639
    def test_expiry_worker_acquires_restore_guard_before_assignment_lock(self):
        operation, service, work = self.admit()
        attempt = service.claim(self.machine_actor,work,request_id=uuid4())
        from mop.server.sessions.models import AssignmentAttempt
        from django.test.utils import CaptureQueriesContext
        AssignmentAttempt.objects.filter(pk=attempt.id).update(lease_until=timezone.now()-timedelta(seconds=1))
        with CaptureQueriesContext(transaction.get_connection()) as queries:
            self.assertEqual(service.reconcile_expired(),(attempt.id,))
        locks = [entry['sql'] for entry in queries.captured_queries if 'FOR UPDATE' in entry['sql'].upper()]
        self.assertTrue(locks)
        self.assertIn(RestoreGuard._meta.db_table,locks[0], 'expiry may not reverse guard -> Assignment -> Operation')

    def test_clone_reassignment_refuses_manual_transaction_before_external_io(self):
        self.fixture()
        from mop.server.sessions.services import Unavailable
        from mop.server.controller.runtime import assignments_service as Assignments
        released = Assignments().release(self.actor,self.target.id,expected_revision=self.target.revision)
        calls = []
        class Clones:
            def observe(inner,**scope):
                calls.append(scope)
                raise AssertionError('external clone query entered under a manual transaction')
        assignments = Assignments(clones=Clones())
        connection = transaction.get_connection()
        connection.set_autocommit(False)
        try:
            with self.assertRaisesRegex(Unavailable,'surrounding transaction'):
                assignments.reassign(self.actor,self.target.id,expected_revision=released.revision,
                    role=self.target.role,bundle_digest=self.target.bundle_digest,grant_id=self.target.grant_id,
                    expires_at=timezone.now()+timedelta(minutes=10))
        finally:
            connection.rollback(); connection.set_autocommit(True)
        self.assertEqual(calls,[])

    def test_machine_claim_requires_real_autocommit_not_a_manual_outer_transaction(self):
        operation, service, work = self.admit()
        from mop.server.sessions.services import Unavailable
        connection = transaction.get_connection()
        connection.set_autocommit(False)
        try:
            with self.assertRaisesRegex(Unavailable,'surrounding transaction'):
                service.claim(self.machine_actor,work,request_id=uuid4())
        finally:
            connection.rollback(); connection.set_autocommit(True)

    def test_read_ports_refuse_a_manual_transaction_before_external_io(self):
        operation, service, work = self.admit()
        attempt = service.claim(self.machine_actor,work,request_id=uuid4())
        service.result(self.machine_actor,attempt.id,expected_revision=1,receipt_id=uuid4(),
            outcome='unknown',evidence='connection-lost')
        from mop.server.sessions.services import Unavailable
        from mop.server.sessions.protocol import FactPageV1, InspectionV1
        calls = []
        class Reader:
            def read(inner,**scope):
                calls.append('facts')
                return FactPageV1(self.node.id,'health',True,())
        class Inspector:
            def inspect(inner,actual,attempt_id):
                calls.append('inspection')
                return InspectionV1(attempt_id,actual.incarnation_id,actual.executor_epoch,
                    'failed','failed-before-effect',True)
        service.reader, service.inspector = Reader(), Inspector()
        connection = transaction.get_connection()
        for name,call in (
                ('facts',lambda: service.facts(self.machine_actor,self.node.id,kind='health')),
                ('inspection',lambda: service.reconcile(self.actor,attempt.id,expected_revision=2,receipt_id=uuid4()))):
            with self.subTest(port=name):
                connection.set_autocommit(False)
                try:
                    with self.assertRaisesRegex(Unavailable,'surrounding transaction'):
                        call()
                finally:
                    connection.rollback(); connection.set_autocommit(True)
        self.assertEqual(calls,[])

    def test_machine_receipt_cannot_ack_an_outer_uncommitted_transaction(self):
        operation, service, work = self.admit()
        attempt = service.claim(self.machine_actor, work, request_id=uuid4())
        from mop.server.sessions.services import Unavailable
        with transaction.atomic():
            with self.assertRaisesRegex(Unavailable, 'surrounding transaction'):
                service.result(self.machine_actor, attempt.id, expected_revision=1, receipt_id=uuid4(),
                    outcome='succeeded', evidence='identity-applied')

    def test_common_node_claim_conflicts_with_running_restart(self):
        operation, service, work = self.admit()
        service.claim(self.machine_actor, work, request_id=uuid4())
        from mop.server.controller.runtime import operations_service
        from mop.server.operations.node import NodeEligibility
        api = operations_service()
        command = NodeEligibility(epoch=self.epoch, request_id=uuid4(), node_id=self.node.id,
            expected_revision=self.node.revision, not_after=timezone.now()+timedelta(minutes=2), eligible=False)
        node_op = api.submit(self.actor, command)
        self.assertIsNone(api.claim(UUID(node_op['id'])))

    def test_receipt_outbox_failure_rolls_back_both_machine_and_common_receipts(self):
        operation, service, work = self.admit()
        attempt = service.claim(self.machine_actor, work, request_id=uuid4())
        from mop.server.operations import models as ops
        from mop.server.sessions import models as sessions
        from unittest.mock import patch
        with patch.object(ops.OutboxEvent.objects, 'create', side_effect=RuntimeError('fixture rollback')):
            with self.assertRaisesRegex(RuntimeError, 'fixture rollback'):
                service.result(self.machine_actor, attempt.id, expected_revision=1, receipt_id=uuid4(),
                    outcome='succeeded', evidence='identity-applied')
        self.assertFalse(ops.EffectReceipt.objects.exists())
        self.assertFalse(sessions.ResultReceipt.objects.exists())
        self.assertEqual(sessions.AssignmentAttempt.objects.get(pk=attempt.id).state, 'claimed')

    def test_unsupported_fresh_and_send_are_refused_before_any_admission(self):
        self.fixture()
        body = self.body(params={'fresh': True})
        response = self.client.post('/api/v1/puppets/'+str(self.target.id)+'/commands/restart', body,
            content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 400, response.content)
        from mop.server.operations.models import Operation
        self.assertEqual(Operation.objects.count(), 0)

    def test_restart_route_admits_committed_common_step_and_machine_projection(self):
        self.fixture()
        body = self.body()
        response = self.client.post('/api/v1/puppets/'+str(self.target.id)+'/commands/restart', body,
            content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 202, response.content)
        from mop.server.operations.models import Operation, Step
        operation = response.json()['operation']
        self.assertEqual(Operation.objects.count(), 1)
        self.assertEqual(Step.objects.count(), 3)
        retry = self.client.get('/api/v1/operations', {'request_id':body['request_id'],'epoch':str(self.epoch)}, secure=True)
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual(retry.json()['operation']['id'], operation['id'])
        from mop.server.controller.machinebinding import compose
        from mop.server.sessions.protocol import WorkV1
        service = compose(grants=self.grants, restart_support=self.support)
        for action, evidence in (('apply_identity','identity-applied'), ('restart_session','session-replaced'), ('confirm_ready','ready')):
            work, = service.poll(self.machine_actor, self.node.id, executor_epoch=self.boot, limit=1)
            self.assertIs(type(work), WorkV1)
            self.assertEqual(str(work.operation_id), operation['id'])
            self.assertEqual(work.target_id, self.target.id)
            self.assertEqual(work.action, action)
            attempt = service.claim(self.machine_actor, work, request_id=uuid4())
            self.assertFalse(transaction.get_connection().in_atomic_block)
            receipt = service.result(self.machine_actor, attempt.id, expected_revision=1, receipt_id=uuid4(),
                outcome='succeeded', evidence=evidence)
        status = self.client.get('/api/v1/operations/'+operation['id'], secure=True)
        self.assertEqual(status.json()['operation']['state'], 'succeeded', status.content)
        self.assertEqual(receipt.outcome, 'succeeded')
        result = status.json()['operation']
        self.assertFalse(result['resources_held'])
        self.assertEqual(result['target']['revision'],str(self.target.revision))
        self.assertNotIn('applied_revision',result['evidence'], 'session receipt is not a catalog revision application')
        self.assertEqual(result['evidence']['action'],'confirm_ready')
        self.assertEqual(result['evidence']['incarnation'],str(self.target.current_incarnation_id))
        self.assertIs(result['evidence']['confirmed'],True)
