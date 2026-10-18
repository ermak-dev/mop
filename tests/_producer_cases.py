"""Регистрация producer #641/#643 на настоящем PostgreSQL 17.

HYPOTHESIS: raw telemetry не имеет authoritative currentSession, а node runtime
не имеет production registration port; hook JSON не может стать authority.
SPEC: enrolled node + canonical RBAC, exact claimed launch/job/fences, SQL CAS,
never-reuse producer epochs, exact ACK, повторная authority перед COMMIT.
Owned native-shaped process/socket + настоящий stdlib hook -- не actual CLI
acceptance, не доказательство model readiness/physical retirement.
RED: 13/13 PG17 -- отсутствует authenticated route (404 вместо 401); затем
отдельный RED -- cached binding возвращает producer уже другого назначения.
SOLUTION: current claimed launch/job/pins/fences, canonical node permission,
immutable never-reuse ledger + SQL CAS и повторная authority до outer COMMIT.
RESULT: 134/134 PG17 producer/telemetry/launch/RBAC/foundation/execution; 8/8
узких node/import suites. Включены concurrent CAS, внешний COMMIT отзыва token,
verified HTTPS и реальный owned tmux/proc/socket/stdlib hook -> SQL ingest.
STATUS: FIXED -- see #641/#643; production composition/actual CLI не приняты.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from unittest.mock import patch
from uuid import UUID, uuid4

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import close_old_connections, connections, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

import _launch_delivery_cases as launch_fixture


# BUG (#716): producer отвергает настоящую передачу claims scheduler -> machine.
# HYPOTHESIS: root ошибочно считает владельцем всех fences scheduler_register;
# apply_identity держит три новых claims, confirm_ready сохраняет их после receipt.
# SOLUTION: общий create-only launch_definition доказывает точные keys/stage/
# token/attempt и immutable receipt предшественника; root не дублирует этот gate.
# RED: PG17 -- обе настоящие передачи дают 409 вместо 200; launch delivery
# выдаёт байты с чужим token. 8 checks, 24 behavioral failures, без setup errors.
# RESULT: 99/99 PG17 producer/launch delivery/create; layers/controller_layers 2/2.
# STATUS: FIXED — see #716
# Owned HTTP scheduler и claimed machine -- не native body/CLI acceptance.
@override_settings(ALLOWED_HOSTS=['testserver'])
class CreateProducerHandoff(TransactionTestCase):
    serialized_rollback = True
    from _create_cases import CreateVertical, CreateRegistration
    setUp = CreateVertical.setUp
    ready, body, submit = CreateVertical.ready, CreateVertical.body, CreateVertical.submit
    launch = CreateRegistration.launch

    def fixture(self):
        from mop.server.access import services as auth
        from mop.server.controller.createbinding import LaunchSupport
        from mop.server.controller.machinebinding import compose
        from mop.server.operations import models as om
        from mop.server.sessions.models import Assignment, IncarnationLaunch
        operation, target, incarnation = self.launch()
        self.ops.launches.run(operation)
        self.assertEqual(len(self.peer.posts), 1)
        self.operation = om.Operation.objects.get(pk=operation)
        self.assignment = Assignment.objects.get(pk=target)
        self.pin = IncarnationLaunch.objects.get(incarnation_id=incarnation)
        self.machine_key = auth.enroll_service('producer-handoff', self.node.pk,
            project_ids=(self.project.pk,), permissions=('execution.poll_work', 'execution.claim_work',
                'execution.report_result', 'execution.register_producer', 'execution.read_launch'))
        self.machine = auth.token_context(self.machine_key)
        self.execution = compose(grants=self.grants, launch_support=LaunchSupport())
        self.claim_next('apply_identity')
        from mop.common.telemetry import SessionScope
        self.scope = SessionScope(self.work.epoch, self.work.target_id, self.work.incarnation_id,
            self.work.node_id, self.work.executor_epoch, 'owned-handoff', uuid4())
        self.document = dict(schema_version=1, work=self.metadata, scope=self.scope.wire(),
            expected_revision='0', expected_prior=None,
            launch=dict(bootstrap_revision=str(self.pin.bootstrap_revision),
                bootstrap_digest=self.pin.bootstrap_digest, image_digest=self.pin.image_digest,
                display_name=self.assignment.display_name),
            physical=dict(pane='%0', pid='123', process_start=str(uuid4())+':123'))

    def claim_next(self, action):
        from mop.server.sessions.protocol import work_to_wire
        self.work, = self.execution.poll(self.machine, self.node.pk, executor_epoch=self.boot)
        self.assertEqual(self.work.action, action)
        self.attempt = self.execution.claim(self.machine, self.work, request_id=uuid4())
        self.metadata = work_to_wire(self.work)

    def post(self):
        return Client(HTTP_AUTHORIZATION='Token '+self.machine_key).post(
            '/api/v1/machine/session-producers', self.document,
            content_type='application/json', secure=True)

    def launch_definition(self):
        with transaction.atomic():
            return self.execution.work.launch_definition(self.work, self.attempt.id)

    def test_real_scheduler_machine_handoff_registers_producer(self):
        from mop.server.operations import models as om
        self.fixture()
        claims = om.ResourceClaim.objects.filter(operation=self.operation, released_at=None)
        self.assertEqual(dict(claims.values_list('resource_id', 'step__name')), {
            'target:'+str(self.assignment.id): 'scheduler_register',
            'node:'+str(self.node.id): 'apply_identity',
            'project:'+str(self.project.id): 'apply_identity',
            'puppet:'+str(self.assignment.id): 'apply_identity'})
        self.assertEqual(self.launch_definition(), self.definition.id)
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), dict(schema_version=1, scope=self.scope.wire(), revision='1'))
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(len(self.peer.posts), 1, 'registration proof must be SQL-only')

    def confirm_ready(self):
        self.execution.result(self.machine, self.attempt.id, expected_revision=self.attempt.revision,
            receipt_id=uuid4(), outcome='succeeded', evidence='identity-applied')
        self.claim_next('confirm_ready')
        self.document['work'] = self.metadata

    def assert_fence_refused(self):
        from mop.server.access.services import Denied
        from mop.server.operations.service import Refused
        from mop.server.sessions.producer_models import CurrentProducer, SessionProducer
        with self.assertRaises((Denied, Refused)):
            self.launch_definition()
        response = self.post()
        self.assertIn(response.status_code, (403, 409), response.content)
        self.assertFalse(CurrentProducer.objects.exists())
        self.assertFalse(SessionProducer.objects.exists())

    def changed(self, rows, **values):
        from contextlib import contextmanager
        @contextmanager
        def change():
            previous = rows.values('pk', *values).get()
            exact = rows.model.objects.filter(pk=previous.pop('pk'))
            exact.update(**values)
            try:
                yield
            finally:
                exact.update(**previous)
        return change()

    def test_every_real_claim_rejects_foreign_operation_token_attempt_or_release(self):
        from mop.server.operations import models as om
        self.fixture()
        foreign = self.submit(self.body())
        self.assertEqual(foreign.status_code, 202, foreign.content)
        foreign_id = UUID(foreign.json()['operation']['id'])
        for claim in om.ResourceClaim.objects.filter(operation=self.operation, released_at=None):
            rows = om.ResourceClaim.objects.filter(pk=claim.pk)
            for changes in ({'operation_id': foreign_id}, {'token': uuid4()}, {'released_at': timezone.now()}):
                with self.subTest(resource=claim.resource_id, changes=changes), self.changed(rows, **changes):
                    self.assert_fence_refused()
            step = om.Step.objects.filter(pk=claim.step_id)
            with self.subTest(resource=claim.resource_id, field='attempt_id'), self.changed(step, attempt_id=uuid4()):
                self.assert_fence_refused()

    def test_historical_claim_requires_exact_dispatched_succeeded_quiescent_stage_and_receipt(self):
        from mop.server.operations import models as om
        self.fixture()
        self.confirm_ready()
        for name in ('scheduler_register', 'apply_identity'):
            step = om.Step.objects.filter(operation=self.operation, name=name)
            for changes in ({'outcome': 'unknown'}, {'outcome': 'failed'}, {'quiescent_at': None},
                    {'dispatched_at': None}, {'name': 'prepare_launch'}, {'ordinal': 5}, {'attempt_id': uuid4()}):
                with self.subTest(stage=name, changes=changes), self.changed(step, **changes):
                    self.assert_fence_refused()
            # Согласованная подмена Step + Claim не подменяет immutable receipt.
            token = uuid4()
            claims = om.ResourceClaim.objects.filter(operation=self.operation, step__name=name, released_at=None)
            with self.subTest(stage=name, changes='matching tokens without their receipt'), \
                    self.changed(step, token=token, attempt_id=token):
                previous = list(claims.values_list('pk', 'token'))
                claims.update(token=token)
                try:
                    self.assert_fence_refused()
                finally:
                    for pk, old_token in previous:
                        om.ResourceClaim.objects.filter(pk=pk).update(token=old_token)

    def test_scheduler_and_machine_fences_are_not_interchangeable(self):
        from mop.server.operations import models as om
        self.fixture()
        scheduler = om.Step.objects.get(operation=self.operation, name='scheduler_register')
        for key, owner, token in (
                ('target:'+str(self.assignment.id), self.work.step_id, self.attempt.id),
                ('node:'+str(self.node.id), scheduler.pk, scheduler.token)):
            claim = om.ResourceClaim.objects.filter(operation=self.operation, resource_id=key, released_at=None)
            with self.subTest(resource=key), self.changed(claim, step_id=owner, token=token):
                self.assert_fence_refused()

    def test_future_or_foreign_stage_cannot_own_a_current_machine_fence(self):
        from mop.server.operations import models as om
        self.fixture()
        claim = om.ResourceClaim.objects.filter(operation=self.operation,
            resource_id='node:'+str(self.node.id), released_at=None)
        future = om.Step.objects.get(operation=self.operation, name='confirm_ready')
        # Даже правдоподобные token/receipt не делают будущую stage предшественником.
        future.token = future.attempt_id = uuid4()
        future.started_at = future.dispatched_at = future.quiescent_at = timezone.now()
        future.outcome = 'succeeded'
        future.save()
        om.EffectReceipt.objects.create(step=future, token=future.token, epoch=self.operation.epoch,
            node_id=self.node.id, source_revision=self.operation.expected_revision,
            decision_revision=self.operation.revision-1, applied=True)
        with self.changed(claim, step_id=future.pk, token=future.token):
            self.assert_fence_refused()
        response = self.submit(self.body())
        foreign = om.Step.objects.get(operation_id=response.json()['operation']['id'], name='apply_identity')
        foreign.token = foreign.attempt_id = uuid4()
        foreign.save()
        with self.changed(claim, step_id=foreign.pk, token=foreign.token):
            self.assert_fence_refused()

    def test_confirm_ready_accepts_quiescent_predecessor_claims(self):
        from mop.server.operations import models as om
        self.fixture()
        self.confirm_ready()
        claims = om.ResourceClaim.objects.filter(operation=self.operation, released_at=None)
        self.assertEqual(set(claims.values_list('step__name', flat=True)),
            {'scheduler_register', 'apply_identity'})
        self.assertFalse(claims.filter(token=self.attempt.id).exists(),
            'later machine stages retain the earlier quiescent claims, not new tokens')
        # Историческая lease уже не живая authority; её заменяет receipt quiescence.
        om.Step.objects.filter(operation=self.operation, name__in=('scheduler_register', 'apply_identity')).update(
            lease_until=self.operation.created_at)
        self.assertEqual(self.launch_definition(), self.definition.id)
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)


@override_settings(ALLOWED_HOSTS=['testserver'])
class Producers(TransactionTestCase):
    serialized_rollback = True
    setUp = launch_fixture.LaunchDelivery.setUp
    reserved_grant = launch_fixture.LaunchDelivery.reserved_grant
    grant_assignment = launch_fixture.LaunchDelivery.grant_assignment
    url = '/api/v1/machine/session-producers'

    def fixture(self):
        self.assertEqual(Client().post(self.url, {}, content_type='application/json', secure=True).status_code,
                         401, 'authenticated finite producer registration route must exist')
        permission = Permission.objects.filter(content_type__app_label='execution', codename='register_producer').first()
        self.assertIsNotNone(permission, 'canonical producer registration permission must exist')
        launch_fixture.LaunchDelivery.fixture(self)
        get_user_model().objects.get(pk=self.machine.user_id).user_permissions.add(permission,
            Permission.objects.get(content_type__app_label='execution', codename='ingest_telemetry'))
        # Исправляем только собственную fixture: scheduler receipt, admission и
        # enrolled node должны называть один и тот же реальный фиксированный job.
        from mop.server.operations import models as om
        from mop.server.sessions.models import NodeExecutor
        binding = om.CreateBinding.objects.get(operation=self.operation)
        binding.applied_index = 1
        binding.save(update_fields=['applied_index'])
        om.ScheduledPuppet.objects.filter(target_id=self.assignment.id).update(scheduler_id=binding.scheduler_id)
        om.ScheduledNode.objects.create(node_id=self.assignment.node_id, scheduler_id=binding.scheduler_id,
            nomad_node_id=uuid4(), capabilities=[])
        NodeExecutor.objects.create(node_id=self.assignment.node_id,
            executor_epoch=self.assignment.current_incarnation.executor_epoch)
        from mop.common.telemetry import SessionScope
        self.scope = SessionScope(UUID(self.metadata['epoch']), self.assignment.id,
            self.reserved.incarnation, self.assignment.node_id,
            UUID(self.metadata['executor_epoch']), 'owned-session', uuid4())
        self.document = dict(schema_version=1, work=self.metadata,
            scope=self.scope.wire(), expected_revision='0', expected_prior=None,
            launch=dict(bootstrap_revision=str(self.pin.bootstrap_revision),
                bootstrap_digest=self.pin.bootstrap_digest, image_digest=self.pin.image_digest,
                display_name=self.assignment.display_name),
            physical=dict(pane='%0', pid='123', process_start=str(uuid4())+':123'))
        return permission

    def post(self, document=None, key=None):
        return Client(HTTP_AUTHORIZATION='Token '+(key or self.machine_key)).post(self.url,
            self.document if document is None else document, content_type='application/json', secure=True)

    def registered(self):
        response = self.post()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), dict(schema_version=1, scope=self.scope.wire(), revision='1'))
        self.assertEqual(response['Cache-Control'], 'no-store')
        return response

    def test_canonical_registration_and_sql_only_telemetry_root(self):
        self.fixture()
        self.registered()
        from mop.server.controller.telemetryroot import current_sessions
        from mop.server.sessions.services import Assignments
        from mop.common.telemetry import CurrentSession, Observation
        binding = Assignments().target(self.assignment.id)
        self.assertEqual(current_sessions.current(binding), CurrentSession(self.scope.session_id, self.scope.producer_epoch))
        event = Observation(self.scope, 1, 'SessionStart', None, None, None, None, 0)
        response = Client(HTTP_AUTHORIZATION='Token '+self.machine_key).post('/api/v1/machine/telemetry',
            event.wire(), content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 200, response.content)
        from mop.server.sessions import models as em
        self.assertEqual(em.TelemetryReceipt.objects.count(), 1)
        self.assertFalse(em.ReadinessReceipt.objects.exists())
        from mop.server.sessions.producer_models import CurrentProducer
        row = CurrentProducer.objects.get()
        revision = row.revision
        forged = replace(event, scope=replace(self.scope, producer_epoch=uuid4()), sequence=2)
        response = Client(HTTP_AUTHORIZATION='Token '+self.machine_key).post('/api/v1/machine/telemetry',
            forged.wire(), content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 409, response.content)
        row.refresh_from_db()
        self.assertEqual(row.revision, revision, 'raw telemetry must never register currentSession')
        hint=Observation(self.scope,2,'Statusline',None,'hint-only-model',None,None,0)
        response=Client(HTTP_AUTHORIZATION='Token '+self.machine_key).post('/api/v1/machine/telemetry',
            hint.wire(),content_type='application/json',secure=True)
        self.assertEqual(response.status_code,200,response.content)
        self.assertFalse(em.ReadinessReceipt.objects.exists(), 'socket/registration/statusline are not readiness')
        from mop.server.controller.telemetryroot import telemetry
        self.assertIsNone(telemetry.latest(self.machine,self.scope).current_model)

    def test_first_retry_renew_replace_and_historical_epoch_never_returns(self):
        self.fixture()
        self.registered()
        self.registered()
        renew = dict(self.document, expected_prior=self.scope.wire(), expected_revision='1')
        self.assertEqual(self.post(renew).json()['revision'], '1')
        second = replace(self.scope, producer_epoch=uuid4(), session_id='session-two')
        replacement = dict(renew, scope=second.wire())
        response = self.post(replacement)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), dict(schema_version=1, scope=second.wire(), revision='2'))
        self.assertEqual(self.post(replacement).json()['revision'], '2')
        self.assertEqual(self.post().status_code, 409, 'old retry must not restore historical producer')
        self.assertEqual(self.post(renew).status_code, 409)
        restore = dict(renew, expected_prior=second.wire(), expected_revision='2')
        self.assertEqual(self.post(restore).status_code, 409, 'even current CAS cannot reuse a retired epoch')
        reused = dict(replacement, scope=replace(second, session_id='different').wire(),
            expected_prior=second.wire(), expected_revision='2')
        self.assertEqual(self.post(reused).status_code, 409)
        from mop.common.telemetry import Observation
        client=Client(HTTP_AUTHORIZATION='Token '+self.machine_key)
        event=Observation(self.scope,1,'SessionStart',None,None,None,None,0)
        self.assertEqual(client.post('/api/v1/machine/telemetry',event.wire(),
            content_type='application/json',secure=True).status_code,409)
        event=replace(event,scope=second)
        self.assertEqual(client.post('/api/v1/machine/telemetry',event.wire(),
            content_type='application/json',secure=True).status_code,200)

    def test_same_scope_with_changed_physical_identity_is_not_an_idempotent_retry(self):
        self.fixture()
        self.registered()
        for name, value in [('pane', '%1'), ('pid', '124'), ('process_start', str(uuid4())+':1')]:
            document = dict(self.document, physical={**self.document['physical'], name:value})
            self.assertEqual(self.post(document).status_code, 409)

    def test_concurrent_first_registration_is_one_commit_and_competing_cas_has_one_winner(self):
        self.fixture()
        barrier = Barrier(2)
        def run(document):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return self.post(document).status_code
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as workers:
            self.assertEqual(list(workers.map(run, [self.document, self.document])), [200, 200])
        variants = [dict(self.document, scope=replace(self.scope, producer_epoch=uuid4()).wire(),
            expected_prior=self.scope.wire(), expected_revision='1') for _ in range(2)]
        with ThreadPoolExecutor(max_workers=2) as workers:
            self.assertEqual(sorted(workers.map(run, variants)), [200, 409])
        from mop.server.sessions.producer_models import CurrentProducer, SessionProducer
        self.assertEqual(CurrentProducer.objects.get().revision, 2)
        self.assertEqual(SessionProducer.objects.count(), 2)

    def test_wrong_node_project_human_and_service_purpose_are_denied(self):
        self.fixture()
        from mop.server.catalog.models import Node, NodeProject, Project
        node = Node.objects.create(name='producer-foreign', driver='host', ssh_host='foreign.invalid',
            control_origin='https://foreign.invalid', lan_address='foreign.invalid', bootstrap_address='foreign.invalid')
        NodeProject.objects.create(node=node, project=self.project)
        other = self.access.enroll_service('producer-foreign', node.id, project_ids=(self.project.id,),
            permissions=('execution.claim_work','execution.register_producer'))
        project = Project.objects.create(name='producer-other', origin='https://example.invalid/other.git')
        NodeProject.objects.create(node_id=self.assignment.node_id, project=project)
        other_project = self.access.enroll_service('producer-other-project', self.assignment.node_id,
            project_ids=(project.id,), permissions=('execution.claim_work','execution.register_producer'))
        for key in (self.token, other, other_project):
            self.assertIn(self.post(key=key).status_code, (403, 404))
        from mop.server.access.models import ServiceEnrollment
        ServiceEnrollment.objects.filter(actor_id=self.machine.user_id).update(use_case='grant-activator')
        self.assertEqual(self.post().status_code, 403)

    def test_missing_permission_and_revoked_token_cannot_register_or_renew(self):
        permission = self.fixture()
        self.registered()
        user = get_user_model().objects.get(pk=self.machine.user_id)
        user.user_permissions.remove(permission)
        self.assertEqual(self.post().status_code, 403)
        user.user_permissions.add(permission)
        self.access.revoke_token(self.machine)
        self.assertIn(self.post().status_code, (401, 403))

    def test_every_work_scope_pin_and_deadline_are_exact(self):
        self.fixture()
        variants = []
        for name in ('epoch','operation_id','step_id','target_id','incarnation_id','node_id','executor_epoch','grant_id'):
            variants.append(dict(self.document, work={**self.metadata, name:str(uuid4())}))
        for name in ('revision','generation','owner_id'):
            variants.append(dict(self.document, work={**self.metadata, name:str(int(self.metadata[name])+1)}))
        for name in ('epoch','target_id','incarnation_id','node_id','executor_epoch'):
            variants.append(dict(self.document, scope={**self.scope.wire(), name:str(uuid4())}))
        for name, value in [('bootstrap_revision',str(uuid4())),('bootstrap_digest','f'*64),
                            ('image_digest','f'*64),('display_name','pu-foreign-1')]:
            variants.append(dict(self.document, launch={**self.document['launch'],name:value}))
        variants.append(dict(self.document, work={**self.metadata,'not_after':timezone.now().isoformat()}))
        for document in variants:
            with self.subTest(document=document):
                self.assertIn(self.post(document).status_code, (400,403,404,409))
        from mop.server.sessions.producer_models import CurrentProducer
        self.assertFalse(CurrentProducer.objects.exists())

    def test_claim_fixed_job_and_resource_fences_are_current(self):
        self.fixture()
        from mop.server.operations import models as om
        from mop.server.sessions import models as em
        checks = [(em.AssignmentAttempt, {'pk':self.attempt.id}, 'state','unknown'),
            (em.NodeExecutor, {'node_id':self.assignment.node_id}, 'executor_epoch',uuid4()),
            (om.Step, {'pk':self.metadata['step_id']}, 'attempt_id',uuid4()),
            (om.ScheduledPuppet, {'target_id':self.assignment.id}, 'incarnation_id',uuid4()),
            (om.ScheduledPuppet, {'target_id':self.assignment.id}, 'job_id','arbitrary-job'),
            (om.ScheduledPuppet, {'target_id':self.assignment.id}, 'scheduler_id',uuid4()),
            (om.ScheduledPuppet, {'target_id':self.assignment.id}, 'modify_index',2),
            (om.ResourceClaim, {'operation':self.operation,'resource_id':'target:'+str(self.assignment.id)},
                'released_at',timezone.now())]
        for model, query, name, value in checks:
            rows = model.objects.filter(**query)
            previous = rows.values_list(name,flat=True).get()
            rows.update(**{name:value})
            try:
                with self.subTest(model=model.__name__, name=name):
                    self.assertIn(self.post().status_code, (403,409))
            finally:
                rows.update(**{name:previous})
        em.AssignmentAttempt.objects.filter(pk=self.attempt.id).update(lease_until=timezone.now())
        self.assertEqual(self.post().status_code, 409)

    def test_projection_never_survives_new_assignment_executor_or_installation(self):
        self.fixture()
        self.registered()
        from mop.server.sessions import models as em
        from mop.server.sessions.services import Assignments
        from mop.server.controller.telemetryroot import current_sessions
        old_binding = Assignments().target(self.assignment.id)
        for model, query, field, value in [(em.Assignment, {'pk':self.assignment.id}, 'generation',2),
            (em.ExecutionIncarnation, {'pk':self.reserved.incarnation}, 'executor_epoch',uuid4())]:
            rows=model.objects.filter(**query)
            previous=rows.values_list(field,flat=True).get()
            rows.update(**{field:value})
            self.assertIsNone(current_sessions.current(Assignments().target(self.assignment.id)))
            self.assertIsNone(current_sessions.current(old_binding), 'caller-cached binding is not SQL authority')
            rows.update(**{field:previous})
        from mop.server.controller.models import Installation
        Installation.objects.filter(pk=1).update(epoch=uuid4())
        self.assertIsNone(current_sessions.current(Assignments().target(self.assignment.id)))

    def test_revocation_between_proof_and_ack_rolls_back_registration(self):
        self.fixture()
        from mop.server.controller import telemetryroot
        gate = telemetryroot.producer_registration.launch
        original = gate.check
        calls=[]
        def check(actor, work, report):
            result = original(actor, work, report)
            calls.append(True)
            if len(calls)==1:
                def revoke():
                    close_old_connections()
                    try:
                        self.access.revoke_token(actor)
                    finally:
                        connections.close_all()
                # Другой настоящий PostgreSQL connection COMMIT-ит отзыв, не
                # тот же transaction/savepoint с последующим rollback.
                with ThreadPoolExecutor(max_workers=1) as worker:
                    worker.submit(revoke).result(timeout=10)
            return result
        service = telemetryroot.producer_registration
        with patch.object(gate, 'check', side_effect=check), patch.object(service, '_check', wraps=service._check) as guard:
            self.assertEqual(self.post().status_code, 403)
        self.assertEqual(guard.call_count, 2, 'authority must be rechecked after proof, before ACK')
        self.assertEqual(len(calls), 1, 'revoked credential must fail before a second launch proof')
        from mop.server.sessions.producer_models import CurrentProducer, SessionProducer
        self.assertFalse(CurrentProducer.objects.exists())
        self.assertFalse(SessionProducer.objects.exists())
        from rest_framework.authtoken.models import Token
        self.assertFalse(Token.objects.filter(user_id=self.machine.user_id).exists(), 'external revocation must stay committed')

    def test_ack_requires_outer_commit_and_failure_rolls_back_projection(self):
        self.fixture()
        from mop.server.controller.telemetryroot import producer_registration
        from mop.server.sessions.producer import RegistrationRequest
        from mop.server.sessions.services import Unavailable
        request=RegistrationRequest.parse(self.document)
        with transaction.atomic():
            with self.assertRaises(Unavailable):
                producer_registration.register(self.machine, request)
        from mop.server.sessions.producer_models import CurrentProducer, SessionProducer
        with patch.object(CurrentProducer.objects, 'create', side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):
                producer_registration.register(self.machine, request)
        self.assertFalse(SessionProducer.objects.exists())
        self.registered()

    def test_bounded_finite_json_rejects_secrets_duplicate_keys_and_implicit_cas(self):
        self.fixture()
        import json
        for document in (dict(self.document,token='must-not-be-accepted'), dict(self.document, expected_revision=True),
            dict(self.document, expected_prior=self.scope.wire()),
            dict(self.document, physical={**self.document['physical'],'peerToken':'forbidden'}),
            dict(self.document,physical={**self.document['physical'],'process_start':'bad'})):
            self.assertEqual(self.post(document).status_code, 400)
        client=Client(HTTP_AUTHORIZATION='Token '+self.machine_key)
        for raw in ('{"schema_version":1,'+json.dumps(self.document)[1:], ' '*8193,
                    '['*1800+'0'+']'*1800):
            self.assertEqual(client.post(self.url,raw,content_type='application/json',secure=True).status_code,400)

    def test_machine_adapter_rejects_every_foreign_ack_and_exact_work_mismatch(self):
        self.fixture()
        from pathlib import Path
        from mop.driver.launch import Registration
        from mop.node.execution_adapters import SessionProof
        from mop.node.http_execution import Work, MachineClient, Refused
        from mop.node.producer_registration import ProducerRegistrationClient
        registration = Registration(Work.parse(self.metadata), str(self.pin.bootstrap_revision),
            self.pin.bootstrap_digest, self.pin.image_digest, self.assignment.display_name)
        proof = SessionProof(registration, '%0', self.scope.session_id, self.scope.producer_epoch,
            Path('/owned/session.sock'), 123, str(uuid4())+':1')
        test = self
        class Transport:
            calls = 0
            response = dict(schema_version=1, scope=self.scope.wire(), revision='1')
            async def request(inner, method, path, payload=None):
                inner.calls += 1
                test.assertEqual((method,path),('POST','session-producers'))
                test.assertEqual(payload['work'],test.metadata)
                return inner.response
        async def exercise():
            transport=Transport()
            machine=MachineClient(transport)
            adapter=ProducerRegistrationClient(machine,registration,expected_prior=None,expected_revision=0)
            test.assertEqual(await adapter.register(proof),proof.scope)
            mismatches=[replace(proof.scope, **{key:uuid4()}).wire() for key in
                ('epoch','target_id','incarnation_id','node_id','executor_epoch','producer_epoch')]
            mismatches.append(replace(proof.scope, session_id='foreign').wire())
            for scope in mismatches:
                transport.response=dict(schema_version=1, scope=scope, revision='1')
                with test.assertRaises(Refused):
                    await adapter.register(proof)
            for revision in ('2','0',True):
                transport.response=dict(schema_version=1,scope=proof.scope.wire(),revision=revision)
                with test.assertRaises(Refused):
                    await adapter.register(proof)
            before=transport.calls
            foreign=replace(proof,registration=replace(registration,
                work=replace(registration.work,step_id=str(uuid4()))))
            with test.assertRaises(Refused):
                await adapter.register(foreign)
            with test.assertRaises(Refused):
                await adapter.register(replace(proof,pid=124))
            test.assertEqual(transport.calls,before)
        async_to_sync(exercise)()

    def test_real_verified_https_registration_uses_only_enrolled_machine_transport(self):
        self.fixture()
        import socket
        import ssl
        import subprocess
        import tempfile
        from pathlib import Path
        from django.core.asgi import get_asgi_application
        import uvicorn
        from mop.driver.launch import Registration
        from mop.node.execution_adapters import SessionProof
        from mop.node.http_execution import Work, MachineClient, HttpsTransport
        from mop.node.producer_registration import ProducerRegistrationClient
        registration=Registration(Work.parse(self.metadata),str(self.pin.bootstrap_revision),
            self.pin.bootstrap_digest,self.pin.image_digest,self.assignment.display_name)
        proof=SessionProof(registration,'%0',self.scope.session_id,self.scope.producer_epoch,
            Path('/owned/session.sock'),123,str(uuid4())+':1')
        async def exercise():
            with tempfile.TemporaryDirectory(prefix='mop-producer-tls-') as directory:
                cert,key=Path(directory)/'cert.pem',Path(directory)/'key.pem'
                subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                    '-keyout',str(key),'-out',str(cert),'-subj','/CN=localhost',
                    '-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],check=True,
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                listener=socket.socket()
                listener.bind(('127.0.0.1',0))
                listener.listen(8)
                https=uvicorn.Server(uvicorn.Config(get_asgi_application(),lifespan='off',
                    log_level='error',access_log=False,ssl_certfile=str(cert),ssl_keyfile=str(key)))
                task=asyncio.create_task(https.serve(sockets=[listener]))
                client=MachineClient(HttpsTransport('https://127.0.0.1:'+str(listener.getsockname()[1]),
                    self.machine_key,tls=ssl.create_default_context(cafile=str(cert))))
                try:
                    async with asyncio.timeout(5):
                        while not https.started:
                            if task.done(): await task
                            await asyncio.sleep(.01)
                    adapter=ProducerRegistrationClient(client,registration,expected_prior=None,expected_revision=0)
                    self.assertEqual(await adapter.register(proof),proof.scope)
                    self.assertEqual(await adapter.register(proof),proof.scope)
                    self.assertEqual(adapter.acknowledgement.revision,1)
                finally:
                    await client.aclose()
                    https.should_exit=True
                    await asyncio.wait_for(task,5)
                    listener.close()
        with override_settings(ALLOWED_HOSTS=['127.0.0.1']):
            async_to_sync(exercise)()
        from mop.server.sessions.producer_models import CurrentProducer
        self.assertEqual(CurrentProducer.objects.count(),1)

    def test_machine_adapter_owned_runtime_and_hook_reach_real_sql_projection(self):
        self.fixture()
        # Используем owned tmux/proc fixture, не выдуманный SessionProof.
        from session_runtime import RuntimeTests
        from mop.driver.launch import Registration, RegisteredLayout
        from mop.node.body_launch import SessionRuntime
        from mop.node.http_execution import Work, MachineClient, Refused
        from mop.node.telemetry import TelemetryClient
        from mop.node.producer_registration import ProducerRegistrationClient
        test=self
        class Transport:
            async def request(inner, method, path, payload=None, params=None):
                test.assertEqual(method,'POST')
                def post():
                    try:
                        response=Client(HTTP_AUTHORIZATION='Token '+test.machine_key).post('/api/v1/machine/'+path,
                            payload,content_type='application/json',secure=True)
                        if response.status_code != 200:
                            raise Refused('owned HTTP refused '+str(response.status_code))
                        return response.json()
                    finally:
                        connections.close_all()
                return await asyncio.to_thread(post)
        async def exercise():
            owned=RuntimeTests('test_preexec_environment_is_inherited_and_startup_is_dropped_not_replayed')
            await owned.asyncSetUp()
            try:
                registration=Registration(Work.parse(self.metadata),str(self.pin.bootstrap_revision),
                    self.pin.bootstrap_digest,self.pin.image_digest,self.assignment.display_name)
                owned.layout=RegisteredLayout(owned.root, registration)
                owned.layout.clone.mkdir(parents=True)
                owned.layout.runtime.mkdir(parents=True,mode=0o700)
                owned.path=owned.layout.runtime/'hook.sock'
                owned.base=['tmux','-S',str(owned.layout.tmux_socket)]
                await owned.runtime.aclose()
                client=MachineClient(Transport())
                adapter=ProducerRegistrationClient(client,registration,expected_prior=None,expected_revision=0)
                owned.runtime=SessionRuntime(adapter,TelemetryClient(client.transport))
                env=owned.prepare()
                await owned.spawn(env)
                bridge=await owned.runtime.attach()
                await owned.hook()
                await bridge.flush()
                test.assertEqual(bridge.buffered,0)
                test.assertIsNone(bridge.last_error)
                await owned.runtime.aclose()
                test.assertEqual(env['MOP_TELEMETRY_SOCKET'],str(owned.path))
            finally:
                await owned.runtime.aclose()
                owned.stop()
                owned.directory.cleanup()
        async_to_sync(exercise)()
        from mop.server.sessions.producer_models import CurrentProducer
        from mop.server.sessions.models import TelemetryReceipt, ReadinessReceipt
        self.assertEqual(CurrentProducer.objects.count(),1)
        producer=CurrentProducer.objects.get().producer
        receipt=TelemetryReceipt.objects.get()
        self.assertEqual(receipt.observation.producer_epoch,producer.producer_epoch)
        self.assertEqual(receipt.observation.session_id,producer.session_id)
        self.assertEqual(TelemetryReceipt.objects.count(),1)
        self.assertFalse(ReadinessReceipt.objects.exists())
