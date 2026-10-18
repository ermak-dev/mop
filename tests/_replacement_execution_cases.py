"""#625: SQL lifecycle замены, не готовый update/recycle физического пула.

HYPOTHESIS: activate не умеет заменить exact source под собственным fence,
а retired source либо непригоден для wipe, либо требует общего resurrection.
SPEC: только trusted SQL port; source CAS, pinned grant/incarnation, confirmed
registration; stop закрывает обычный current, оставляя exact recycle wipe.
Фикстура Root ниже читает настоящий журнал PostgreSQL; это не native capability.
RED: PG17 -- 10 assertion failures отсутствующего public lifecycle; затем
отдельно воспроизведены partial commit при receipt collision и shorter-expiry replay.
SOLUTION: typed SQL Root proof, immutable stop/cutover receipts и narrow
stopped-source gate, не ослабляющий current или ordinary grant authority.
RESULT: Python 3.12/PostgreSQL 17 -- 18/18 новых checks и 118/118 combined
execution/stop/wipe/create/preparation/registration checks; layers -- 2/2.
STATUS: FIXED — see #625 (domain prerequisite, не готовый update/recycle).
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

import _execution_cases as execution_fixture
import _replacement_cases as replacement_fixture
from _execution_cases import FakeWork


class ReplacementExecution(TransactionTestCase):
    serialized_rollback = True
    setUp = execution_fixture.MachineExecution.setUp

    def domain(self):
        from mop.server.sessions import protocol
        from mop.server.sessions import services
        self.assertTrue(hasattr(services.Assignments, 'replacement_execution'),
                        'public execution replacement lifecycle must exist without operations ORM imports')
        self.p = protocol
        self.source = services.Assignments().current(project_id=self.project.pk, target_id=self.target.pk)
        self.operation_id, self.step_id, self.token = uuid4(), uuid4(), uuid4()
        self.destination, self.destination_grant = uuid4(), uuid4()
        self.receipt = uuid4()
        self.phase, self.action = 'cutover', 'update'
        self.held, self.extra_held = True, False
        self.port_calls = []
        from mop.server.operations import models as om
        self.om = om
        from mop.server.controller.models import RestoreGuard
        from mop.server.operations.service import _binding
        self.om.Operation.objects.create(id=self.operation_id, actor=self.human, actor_binding=_binding(self.owner),
            request_id=uuid4(), digest='d'*64, epoch=self.epoch,
            restore_generation=RestoreGuard.objects.get(pk=1).generation, action='update', state='running',
            node=self.node, target_kind='puppet', target_id=self.target.pk, incarnation_id=self.destination,
            expected_revision=self.target.revision+1, not_after=self.work.not_after)
        self.om.RestartBinding.objects.create(operation_id=self.operation_id, project=self.project,
            executor_epoch=self.boot, owner_id=self.human.pk, generation=self.target.generation+1,
            role=self.target.role, bundle_digest=self.target.bundle_digest, grant_id=self.destination_grant)
        self.proof = protocol.ReplacementAuthority(self.operation_id, self.step_id, self.token,
            self.epoch, self.action, self.phase, self.source, self.target.expires_at,
            self.destination, self.destination_grant, self.target.generation+1,
            self.target.revision+1, self.target.expires_at,
            protocol.ReplacementLaunch(uuid4(), 'b'*64, 'c'*64, None),
            self.receipt, 8)
        outer = self
        class Port:
            def require(inner, *, operation_id, step_id, token, phase):
                outer.assertTrue(connection.in_atomic_block)
                outer.port_calls.append((operation_id, step_id, token, phase))
                if (operation_id, step_id, token, phase) != (outer.operation_id, outer.step_id, outer.token, outer.phase):
                    raise services.Conflict('exact live operation stage token required')
                # Root fixture: реальные short locks и COMMIT, не mock ORM.
                outer.m.Assignment.objects.select_for_update().get(pk=outer.target.pk)
                row = outer.om.Operation.objects.select_for_update().get(pk=operation_id)
                step = outer.om.Step.objects.select_for_update().get(pk=step_id, operation=row)
                claims = outer.om.ResourceClaim.objects.filter(operation=row, released_at=None)
                binding = outer.om.RestartBinding.objects.get(operation=row)
                if (not outer.held or step.token != token or row.state != 'running'
                        or not claims.exists() or claims.exclude(step=step, token=token).exists()
                        or binding.grant_id != outer.proof.destination_grant_id
                        or row.incarnation_id != outer.proof.destination_incarnation_id):
                    raise services.Conflict('operation, destination grant or exact target fences changed')
                return outer.proof
            def stopped(inner, work):
                if (work.operation_id, work.step_id) != (outer.operation_id, outer.step_id):
                    raise services.Conflict('exact recycle wipe operation required')
                return inner.require(operation_id=work.operation_id, step_id=work.step_id,
                                     token=outer.token, phase='wipe')
        def fence(target_id, *, excluding_operation=None):
            from mop.server.operations.resources import target_held
            outer.assertEqual(target_id, outer.target.pk)
            return outer.extra_held or outer.held and target_held(target_id, excluding_operation=excluding_operation)
        self.fence, self.port_type = fence, Port
        self.stage()
        self.lifecycle = services.Assignments(target_fence=fence).replacement_execution(
            authority=Port(), installation=lambda: protocol.AuthorityV1(self.epoch, True))
        return self.lifecycle

    def stage(self):
        name = {'stop': 'scheduler_stop', 'cutover': 'scheduler_register', 'wipe': 'wipe_clone'}[self.phase]
        self.om.Step.objects.update_or_create(pk=self.step_id, defaults=dict(operation_id=self.operation_id,
            ordinal={'stop': 2, 'cutover': 4, 'wipe': 3}[self.phase], name=name, token=self.token,
            attempt_id=self.token, started_at=timezone.now(), dispatched_at=timezone.now(),
            lease_until=self.work.not_after))
        key = 'target:'+str(self.target.pk)
        self.om.Resource.objects.get_or_create(key=key)
        self.om.ResourceClaim.objects.update_or_create(operation_id=self.operation_id, resource_id=key,
            defaults=dict(step_id=self.step_id, token=self.token))

    def transition(self):
        self.stage()
        with transaction.atomic():
            return getattr(self.lifecycle, self.phase)(operation_id=self.operation_id,
                step_id=self.step_id, token=self.token)

    def recycle(self):
        self.domain()
        self.action, self.phase = 'recycle', 'stop'
        self.proof = replace(self.proof, action='recycle', phase='stop',
            destination_generation=self.source.generation+2, destination_revision=self.source.revision+2)
        self.transition()
        self.target.refresh_from_db()
        return self.target

    def wipe_work(self):
        self.phase = 'wipe'
        self.step_id, self.token = uuid4(), uuid4()
        self.proof = replace(self.proof, phase='wipe', step_id=self.step_id, token=self.token,
                             receipt_id=self.receipt, modify_index=8)
        self.stage()
        return replace(self.work, action='wipe_clone', operation_id=self.operation_id, step_id=self.step_id,
                       generation=self.target.generation, revision=self.target.revision)

    def test_public_lifecycle_is_default_deny_and_requires_caller_transaction(self):
        self.domain()
        unbound = self.s.Assignments(target_fence=self.fence).replacement_execution()
        with transaction.atomic(), self.assertRaises(self.s.Unavailable):
            unbound.cutover(operation_id=self.operation_id, step_id=self.step_id, token=self.token)
        with self.assertRaises(self.s.Unavailable):
            self.lifecycle.cutover(operation_id=self.operation_id, step_id=self.step_id, token=self.token)
        self.assertEqual(self.m.ExecutionIncarnation.objects.count(), 1)

    def test_update_cutover_keeps_assignment_owner_node_and_pinned_distinct_destination(self):
        self.domain()
        before = self.m.Assignment.objects.values().get(pk=self.target.pk)
        result = self.transition()
        self.target.refresh_from_db()
        self.assertEqual((result.incarnation_id, result.grant_id), (self.destination, self.destination_grant))
        self.assertEqual((self.target.owner_id, self.target.project_id, self.target.node_id, self.target.expires_at),
            (before['owner_id'], before['project_id'], before['node_id'], before['expires_at']))
        self.assertEqual((self.target.revision, self.target.generation),
                         (self.source.revision+1, self.source.generation+1))
        self.assertFalse(self.m.ExecutionIncarnation.objects.get(pk=self.inc.id).accepting)
        self.assertEqual(self.m.IncarnationLaunch.objects.get(pk=self.destination).bootstrap_digest, 'b'*64)
        self.assertEqual(self.transition(), result, 'same confirmed receipt is idempotent')

    def test_locator_wrong_operation_step_token_and_foreign_fence_never_cut_over(self):
        self.domain()
        for field in ('operation_id', 'step_id', 'token'):
            args = dict(operation_id=self.operation_id, step_id=self.step_id, token=self.token)
            args[field] = uuid4()
            with transaction.atomic(), self.assertRaises(self.s.Conflict):
                self.lifecycle.cutover(**args)
        self.extra_held = True
        with self.assertRaises(self.s.Conflict):
            self.transition()
        self.extra_held, self.held = False, False
        with self.assertRaises(self.s.Conflict):
            self.transition()
        self.assertEqual(self.m.ExecutionIncarnation.objects.count(), 1)

    def test_wrong_source_owner_project_node_revision_generation_epoch_or_grant_is_denied(self):
        self.domain()
        original = self.proof
        for field, value in (('incarnation_id', uuid4()), ('owner_id', self.human.pk+100),
                ('project_id', uuid4()), ('node_id', uuid4()), ('revision', self.source.revision+1),
                ('generation', self.source.generation+1), ('executor_epoch', uuid4()), ('grant_id', uuid4())):
            with self.subTest(field=field):
                self.proof = replace(original, source=replace(self.source, **{field: value}))
                with self.assertRaises(self.s.Conflict):
                    self.transition()
        self.assertEqual(self.m.ExecutionIncarnation.objects.count(), 1)

    def test_unconfirmed_registration_or_reused_destination_or_wrong_counters_denied(self):
        self.domain()
        original = self.proof
        for changes in ({'receipt_id': None}, {'modify_index': 0},
                {'destination_incarnation_id': self.inc.id}, {'destination_grant_id': self.source.grant_id},
                {'destination_revision': self.source.revision+2},
                {'expires_at': self.target.expires_at+timedelta(seconds=1)}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, self.s.Conflict)):
                self.proof = replace(original, **changes)
                self.transition()
        self.assertEqual(self.m.ExecutionIncarnation.objects.count(), 1)

    def test_delayed_old_receipt_never_rebinds_a_newer_assignment(self):
        self.domain()
        self.transition()
        self.m.Assignment.objects.filter(pk=self.target.pk).update(revision=99, generation=99, grant_id=uuid4())
        with self.assertRaises(self.s.Conflict):
            self.transition()
        self.target.refresh_from_db()
        self.assertEqual((self.target.revision, self.target.generation), (99, 99))

    def test_recycle_stop_closes_normal_current_and_only_exact_wipe_is_admitted(self):
        self.recycle()
        self.assertIsNone(self.s.Assignments().current(project_id=self.project.pk, target_id=self.target.pk))
        self.assertFalse(self.m.ExecutionIncarnation.objects.get(pk=self.inc.id).accepting)
        self.assertEqual((self.target.revision, self.target.generation),
                         (self.source.revision+1, self.source.generation+1))
        work = self.wipe_work()
        service = self.s.MachineExecution(work=FakeWork(work), grants=self.grants, target_fence=self.fence,
            installation=lambda: self.p.AuthorityV1(self.epoch, True), stopped_sources=self.lifecycle)
        attempt = service.claim(self.actor, work, request_id=uuid4())
        self.assertEqual(service.action_work(self.actor, operation_id=work.operation_id, step_id=work.step_id), work)
        receipt = service.result(self.actor, attempt.id, expected_revision=1, receipt_id=uuid4(),
                                 outcome='succeeded', evidence='clone-wiped')
        self.assertEqual(receipt.outcome, 'succeeded')
        self.assertFalse(self.m.ExecutionIncarnation.objects.get(pk=self.inc.id).accepting)
        self.assertIsNone(self.s.Assignments().current(project_id=self.project.pk, target_id=self.target.pk))

    def test_stopped_wipe_has_no_global_exception_and_requires_current_authority(self):
        self.recycle()
        work = self.wipe_work()
        for candidate, lifecycle in ((work, None), (replace(work, action='restart_session'), self.lifecycle),
                (replace(work, operation_id=uuid4()), self.lifecycle), (replace(work, step_id=uuid4()), self.lifecycle)):
            service = self.s.MachineExecution(work=FakeWork(candidate), grants=self.grants, target_fence=self.fence,
                installation=lambda: self.p.AuthorityV1(self.epoch, True), stopped_sources=lifecycle)
            with self.assertRaises((self.s.Conflict, self.s.Unavailable)):
                service.claim(self.actor, candidate, request_id=uuid4())
        self.held = False
        service.stopped_sources = self.lifecycle
        with self.assertRaises(self.s.Conflict):
            service.claim(self.actor, work, request_id=uuid4())
        self.assertEqual(self.m.AssignmentAttempt.objects.count(), 0)

    def test_recycle_cutover_requires_the_same_stop_and_newer_confirmed_register(self):
        self.recycle()
        self.phase, self.step_id, self.token = 'cutover', uuid4(), uuid4()
        original = replace(self.proof, phase=self.phase, step_id=self.step_id, token=self.token,
                           receipt_id=uuid4(), modify_index=9)
        for changes in ({'destination_grant_id': uuid4()}, {'destination_incarnation_id': uuid4()}, {'modify_index': 8}):
            with self.subTest(changes=changes), self.assertRaises(self.s.Conflict):
                self.proof = replace(original, **changes)
                self.transition()
        self.proof = original
        result = self.transition()
        self.assertEqual((result.revision, result.generation),
                         (self.source.revision+2, self.source.generation+2))
        self.assertNotEqual(result.grant_id, self.source.grant_id)

    def test_standalone_stop_without_this_recycle_receipt_does_not_authorize_wipe(self):
        self.domain()
        with transaction.atomic():
            self.s.Assignments().confirm_job_stopped(target_id=self.target.pk, incarnation_id=self.inc.id,
                expected_revision=self.source.revision, expected_generation=self.source.generation,
                executor_epoch=self.boot)
        self.target.refresh_from_db()
        self.proof = replace(self.proof, action='recycle',
            destination_generation=self.source.generation+2, destination_revision=self.source.revision+2)
        work = self.wipe_work()
        with transaction.atomic(), self.assertRaises(self.s.Conflict):
            self.lifecycle.authorize(work)
        self.assertFalse(self.m.ReplacementReceipt.objects.exists())

    def test_disabled_or_changed_installation_and_untyped_port_deny_before_transition(self):
        self.domain()
        for state in (self.p.AuthorityV1(self.epoch, False), self.p.AuthorityV1(uuid4(), True)):
            self.lifecycle.installation = lambda: state
            with self.assertRaises((self.s.Conflict, self.s.Unavailable)):
                self.transition()
        self.lifecycle.installation = lambda: self.p.AuthorityV1(self.epoch, True)
        self.proof = {'operation_id': str(self.operation_id), 'confirmed': True}
        # Сам Root fixture не принимает JSON proof -- подменяем только return,
        # чтобы проверить предметную границу против ошибочного adapter.
        class Untyped:
            def require(inner, **locator):
                return self.proof
        self.lifecycle.port = Untyped()
        with self.assertRaises(self.s.Unavailable):
            self.transition()
        self.assertEqual(self.m.ExecutionIncarnation.objects.count(), 1)

    def test_shorter_pinned_expiry_is_replayable_without_extending_assignment(self):
        self.domain()
        self.proof = replace(self.proof, expires_at=self.target.expires_at-timedelta(seconds=1))
        result = self.transition()
        self.assertEqual(self.transition(), result)
        self.target.refresh_from_db()
        self.assertEqual(self.target.expires_at, self.proof.expires_at)

    def test_receipt_collision_caught_by_caller_cannot_commit_partial_cutover(self):
        self.domain()
        self.m.ReplacementReceipt.objects.create(operation_id=uuid4(), phase='cutover', step_id=uuid4(),
            token=uuid4(), receipt_id=self.receipt, binding_digest='f'*64, assignment=self.target,
            source_incarnation_id=self.inc.id, resulting_revision=3, resulting_generation=3, modify_index=8)
        with transaction.atomic():
            try:
                self.lifecycle.cutover(operation_id=self.operation_id, step_id=self.step_id, token=self.token)
            except self.s.Conflict:
                pass
            else:
                self.fail('a receipt already used by another operation must be refused')
        self.target.refresh_from_db()
        self.assertEqual(self.target.current_incarnation_id, self.inc.id,
                         'caller handling a refusal must not commit partial destination changes')
        self.assertEqual(self.m.ExecutionIncarnation.objects.count(), 1)

    def test_wrong_destination_binding_and_lost_stage_token_deny_claim_and_result(self):
        self.recycle()
        work = self.wipe_work()
        service = self.s.MachineExecution(work=FakeWork(work), grants=self.grants, target_fence=self.fence,
            installation=lambda: self.p.AuthorityV1(self.epoch, True), stopped_sources=self.lifecycle)
        self.om.RestartBinding.objects.filter(operation_id=self.operation_id).update(grant_id=uuid4())
        with self.assertRaises(self.s.Conflict):
            service.claim(self.actor, work, request_id=uuid4())
        self.om.RestartBinding.objects.filter(operation_id=self.operation_id).update(grant_id=self.destination_grant)
        attempt = service.claim(self.actor, work, request_id=uuid4())
        self.om.Step.objects.filter(pk=self.step_id).update(token=uuid4())
        with self.assertRaises(self.s.Conflict):
            service.result(self.actor, attempt.id, expected_revision=1, receipt_id=uuid4(),
                           outcome='succeeded', evidence='clone-wiped')
        self.assertFalse(self.m.ResultReceipt.objects.exists())
        self.assertEqual(self.m.AssignmentAttempt.objects.get(pk=attempt.id).state, 'claimed')

    def test_stopped_source_never_accepts_normal_wipe_or_resurrected_accepting_flag(self):
        self.recycle()
        work = self.wipe_work()
        self.m.ExecutionIncarnation.objects.filter(pk=self.inc.id).update(accepting=True)
        with transaction.atomic(), self.assertRaises(self.s.Conflict):
            self.lifecycle.authorize(work)
        self.m.ExecutionIncarnation.objects.filter(pk=self.inc.id).update(accepting=False)
        self.proof = replace(self.proof, receipt_id=uuid4())
        with transaction.atomic(), self.assertRaises(self.s.Conflict):
            self.lifecycle.authorize(work)

    def test_delayed_stop_receipt_after_cutover_cannot_retire_destination(self):
        self.recycle()
        stopped_proof = self.proof
        self.phase, self.step_id, self.token = 'cutover', uuid4(), uuid4()
        self.proof = replace(self.proof, phase=self.phase, step_id=self.step_id, token=self.token,
                             receipt_id=uuid4(), modify_index=9)
        result = self.transition()
        self.proof = stopped_proof
        self.phase, self.step_id, self.token = 'stop', stopped_proof.step_id, stopped_proof.token
        with self.assertRaises(self.s.Conflict):
            self.transition()
        self.target.refresh_from_db()
        self.assertEqual(self.target.current_incarnation_id, result.incarnation_id)
        self.assertTrue(self.m.ExecutionIncarnation.objects.get(pk=result.incarnation_id).accepting)

    def test_competing_cutover_has_one_destination_and_release_stays_blocked(self):
        self.domain()
        barrier = Barrier(2)
        def worker(_):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                with transaction.atomic():
                    self.lifecycle.cutover(operation_id=self.operation_id, step_id=self.step_id, token=self.token)
                return True
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as workers:
            self.assertEqual(list(workers.map(worker, range(2))), [True, True])
        self.assertEqual(self.m.ExecutionIncarnation.objects.count(), 2)
        self.target.refresh_from_db()
        with self.assertRaises(self.s.Conflict):
            self.s.Assignments(target_fence=self.fence).release(self.owner, self.target.pk,
                expected_revision=self.target.revision)


class ReplacementGrantExecution(TransactionTestCase):
    """Настоящие reserved/active grants не активируются SQL cutover сами собой."""
    serialized_rollback = True
    fixture_class = replacement_fixture.ReplacementPreparation
    setUp, fixture, prepare = fixture_class.setUp, fixture_class.fixture, fixture_class.prepare
    ready, body, submit = fixture_class.ready, fixture_class.body, fixture_class.submit
    launch, step, machine = fixture_class.launch, fixture_class.step, fixture_class.machine
    through_readiness, activator = fixture_class.through_readiness, fixture_class.activator

    def test_cutover_preserves_real_grant_lifecycle_agent_and_token(self):
        from mop.server.access.models import Grant, AgentProfile
        from mop.server.operations import models as om
        from mop.server.operations.resources import target_held
        from mop.server.sessions import protocol as p
        from mop.server.sessions.models import IncarnationLaunch
        from mop.server.sessions.services import Assignments, Conflict
        from rest_framework.authtoken.models import Token
        self.fixture()
        self.prepare()
        source = Assignments().current(project_id=self.project.pk, target_id=self.source.pk)
        original = Grant.objects.get(pk=source.grant_id)
        destination = Grant.objects.select_related('intent').get(pk=self.new_grant_id)
        before = list(Grant.objects.order_by('id').values())
        tokens = list(Token.objects.order_by('key').values())
        profiles = list(AgentProfile.objects.order_by('id').values())
        launch = IncarnationLaunch.objects.get(pk=source.incarnation_id)
        stage = om.Step.objects.get(operation=self.operation, name='scheduler_register')
        stage.token, stage.outcome, stage.evidence = uuid4(), 'succeeded', 'job-registered'
        stage.dispatched_at = stage.quiescent_at = timezone.now()
        stage.save()
        om.ResourceClaim.objects.filter(operation=self.operation).update(step=stage, token=stage.token)
        proof = p.ReplacementAuthority(self.operation.pk, stage.pk, stage.token, self.epoch, 'update', 'cutover',
            source, self.source.expires_at, self.operation.incarnation_id, destination.pk,
            source.generation+1, source.revision+1, destination.intent.expires_at,
            p.ReplacementLaunch(launch.bootstrap_revision, launch.bootstrap_digest, launch.image_digest,
                                launch.image_revision_id), uuid4(), 9)
        outer = self
        class ReadbackSQL:
            # Явный тестовый Root proof: подтверждение stage подставляет fixture,
            # не Nomad. Grant binding и состояния читаются из настоящей БД.
            def require(inner, *, operation_id, step_id, token, phase):
                outer.assertTrue(connection.in_atomic_block)
                outer.source.__class__.objects.select_for_update().get(pk=source.target_id)
                op = om.Operation.objects.select_for_update().get(pk=operation_id)
                step = om.Step.objects.select_for_update().get(pk=step_id, operation=op)
                grant = Grant.objects.select_related('intent').get(pk=proof.destination_grant_id)
                if (phase != 'cutover' or token != step.token or step.outcome != 'succeeded'
                        or step.dispatched_at is None or step.quiescent_at is None
                        or (grant.state, grant.intent.target_id, grant.intent.incarnation, grant.intent.owner_id,
                            grant.intent.project_id) != ('reserved', source.target_id,
                            proof.destination_incarnation_id, source.owner_id, source.project_id)
                        or grant.credential_version_id is None or grant.subject_id != original.subject_id):
                    raise Conflict('exact reserved destination grant and registration readback required')
                return proof
        lifecycle = Assignments(target_fence=target_held).replacement_execution(authority=ReadbackSQL(),
            installation=lambda: p.AuthorityV1(self.epoch, True))
        # Чужой incarnation при том же UUID grant не становится destination.
        from mop.server.access import models as am
        # Intent immutable: новый ложный proof проверяет несовпадение, без
        # SQL обхода immutable manager или порчи настоящего grant.
        valid = proof
        proof = replace(proof, destination_incarnation_id=uuid4())
        with transaction.atomic(), self.assertRaises(Conflict):
            lifecycle.cutover(operation_id=self.operation.pk, step_id=stage.pk, token=stage.token)
        proof = valid
        with transaction.atomic():
            result = lifecycle.cutover(operation_id=self.operation.pk, step_id=stage.pk, token=stage.token)
        self.assertEqual((result.incarnation_id, result.grant_id), (self.operation.incarnation_id, destination.pk))
        self.assertEqual(list(am.Grant.objects.order_by('id').values()), before)
        self.assertEqual(list(Token.objects.order_by('key').values()), tokens)
        self.assertEqual(list(AgentProfile.objects.order_by('id').values()), profiles)
        self.assertEqual(Grant.objects.get(pk=destination.pk).state, 'reserved')
        self.assertEqual(Grant.objects.get(pk=source.grant_id).state, 'active')
