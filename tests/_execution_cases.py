"""Поведенческий контракт #639; настоящие independent PostgreSQL connections."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import close_old_connections
from django.test import TransactionTestCase, Client, override_settings
from django.utils import timezone
from mop.server.controller.runtime import assignments_service, machine_execution_service


@override_settings(ALLOWED_HOSTS=['testserver'])
class MachineHttpBoundary(TransactionTestCase):
    def test_machine_mutation_requires_authenticated_current_service(self):
        response = Client().post('/api/v1/machine/work/claim',
            {'schema_version': 1, 'shell': 'id'}, content_type='application/json', secure=True)
        self.assertIn(response.status_code, (401, 403),
                      'machine claim must pass through live authentication, not an absent route')


# FEATURE: назначения человека отделены от машинного допуска и результата.
# HYPOTHESIS: текущий сервер не хранит admission/fence/receipt в общей БД.
# SPEC: lease timeout/restart не доказывает конец эффекта; replay запрещён.
# RED: PG16 — machine claim 404 вместо live authentication refusal; schema отсутствовала.
# SOLUTION: нормализованные ORM fences, короткие sync use cases и strict v1 DRF.
# RED: CI 5491 (b3059182), затем PG16/17 воспроизвели CAS/scope/recovery гонки.
# RESULT: PG16 и PG17 — по 163 проверки green, включая независимые connections.
# STATUS: FIXED — see #639; реальные adapters и controlled restart проверяются отдельно.
# Проверка app — assertion отсутствующей возможности, не import/setup failure.
class MachineExecution(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        self.assertTrue(apps.is_installed('mop.server.sessions'),
                        'machine execution schema and services must be installed')
        from mop.server.sessions import models, services, protocol
        from mop.server.access import models as a, services as auth
        from mop.server.catalog.models import Node, Project, NodeProject
        from mop.server.controller.runtime import initialize
        initialize()
        from mop.server.controller.models import Installation
        Installation.objects.update(effects_enabled=True)
        self.epoch = Installation.objects.get().epoch
        self.m, self.s, self.p, self.auth = models, services, protocol, auth
        self.project = Project.objects.create(name='one', origin='https://example.invalid/one.git')
        self.node = Node.objects.create(name='node', driver='host', ssh_host='example.invalid',
            control_origin='https://example.invalid', lan_address='example.invalid', bootstrap_address='example.invalid')
        NodeProject.objects.create(node=self.node, project=self.project)
        source = a.IdentitySource.objects.create(name='local', kind='local', priority=0)
        self.human = get_user_model().objects.create_user('owner')
        a.Principal.objects.create(user=self.human, kind='human')
        a.LoginIdentity.objects.create(user=self.human, source=source, external_subject='owner')
        a.ProjectMembership.objects.create(user=self.human, project=self.project, source=source)
        self.human.user_permissions.add(Permission.objects.get(content_type__app_label='execution', codename='assign_work'))
        self.human.user_permissions.add(*Permission.objects.filter(content_type__app_label='execution',
            codename__in=['acquire_landing', 'release_landing']))
        self.owner = auth.token_context(auth.issue_token(self.human.pk))
        self.key = auth.enroll_service('executor', self.node.pk, project_ids=[self.project.pk],
            permissions=['execution.poll_work', 'execution.claim_work', 'execution.report_result', 'execution.read_facts'])
        self.actor = auth.token_context(self.key)
        self.boot = uuid4()
        self.target = assignments_service().create(self.owner, project_id=self.project.pk,
            node_id=self.node.pk, role='worker', bundle_digest='a'*64, grant_id=uuid4(),
            expires_at=timezone.now()+timedelta(hours=1))
        self.inc = assignments_service().activate(self.owner, self.target.id,
            expected_revision=1, executor_epoch=self.boot)
        self.target = self.m.Assignment.objects.get(pk=self.target.id)
        self.work = protocol.WorkV1(epoch=self.epoch, owner_id=self.human.id,
            operation_id=uuid4(), step_id=uuid4(), target_id=self.target.id,
            incarnation_id=self.inc.id, node_id=self.node.id, executor_epoch=self.boot,
            generation=self.target.generation, revision=self.target.revision,
            role='worker', bundle_digest='a'*64, grant_id=self.target.grant_id,
            action='restart_session', not_after=timezone.now()+timedelta(minutes=2))
        self.port = FakeWork(self.work)
        self.grants = FakeGrants()
        self.service = machine_execution_service(work=self.port, grants=self.grants,
            installation=lambda: protocol.AuthorityV1(self.epoch, True))

    def claim(self, request_id=None):
        return self.service.claim(self.actor, self.work, request_id=request_id or uuid4())

    def test_claim_is_durable_and_receipt_retry_is_idempotent(self):
        request = uuid4()
        attempt = self.claim(request)
        self.service = machine_execution_service(work=self.port, grants=self.grants,
            installation=lambda: self.p.AuthorityV1(self.epoch, True))
        self.assertEqual(self.claim(request).id, attempt.id)
        self.assertEqual(self.m.AssignmentAttempt.objects.count(), 1)
        receipt_id = uuid4()
        receipt = self.service.result(self.actor, attempt.id, expected_revision=1,
            receipt_id=receipt_id, outcome='succeeded', evidence='session-replaced')
        again = self.service.result(self.actor, attempt.id, expected_revision=1,
            receipt_id=receipt_id, outcome='succeeded', evidence='session-replaced')
        self.assertEqual(receipt, again)
        self.assertEqual(len(self.port.completed), 1)
        with self.assertRaises(self.s.Conflict):
            self.service.result(self.actor, attempt.id, expected_revision=1,
                receipt_id=receipt_id, outcome='failed', evidence='changed')

    def test_competing_claims_have_one_winner_on_independent_connections(self):
        barrier = Barrier(2)
        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    return self.claim().id
                except self.s.Conflict:
                    return None
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: worker(), range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(self.m.AssignmentAttempt.objects.count(), 1)

    def test_work_port_integrity_failure_is_not_mislabeled_as_claim_contention(self):
        from django.db import IntegrityError
        class BrokenWork(FakeWork):
            def claimed(inner, work, attempt_id):
                raise IntegrityError('injected operation constraint failure')
        self.service.work = BrokenWork(self.work)
        with self.assertRaises(IntegrityError):
            self.claim()
        self.assertEqual(self.m.AssignmentAttempt.objects.count(), 0)

    def test_result_port_integrity_failure_rolls_back_without_fabricated_receipt_collision(self):
        from django.db import IntegrityError
        attempt = self.claim()
        class BrokenResult(FakeWork):
            def completed_result(inner, work, attempt_id, outcome, evidence):
                raise IntegrityError('injected operation result constraint failure')
        self.service.work = BrokenResult(self.work)
        with self.assertRaises(IntegrityError):
            self.service.result(self.actor, attempt.id, expected_revision=1,
                receipt_id=uuid4(), outcome='succeeded', evidence='session-replaced')
        self.assertEqual(self.m.ResultReceipt.objects.count(), 0)
        self.assertEqual(self.m.AssignmentAttempt.objects.get(pk=attempt.id).state, 'claimed')

    def test_claim_cannot_return_an_execution_permit_from_an_uncommitted_outer_transaction(self):
        from django.db import transaction
        with transaction.atomic():
            with self.assertRaises(self.s.Unavailable):
                self.claim()
            self.assertEqual(self.m.AssignmentAttempt.objects.count(), 0)
        self.assertEqual(self.port.started, 0)

    def test_timeout_holds_conflicting_resource_and_never_replays(self):
        attempt = self.claim()
        self.m.AssignmentAttempt.objects.filter(pk=attempt.id).update(lease_until=timezone.now()-timedelta(seconds=1))
        self.service.reconcile_expired(limit=10)
        attempt = self.m.AssignmentAttempt.objects.get(pk=attempt.id)
        self.assertEqual(attempt.state, 'unknown')
        with self.assertRaises(self.s.Conflict):
            self.claim()
        self.assertEqual(self.port.started, 1)

    def test_existing_target_reassignment_requires_fresh_clean_clone_and_fences_old_rollback(self):
        assignments_service().release(self.owner, self.target.id, expected_revision=self.target.revision)
        target = self.m.Assignment.objects.get(pk=self.target.id)
        self.assertTrue(callable(getattr(assignments_service(), 'reassign', None)),
                        'human assignment needs a protected existing-target reassign use case')
        now = timezone.now()
        class Clone:
            clean = False
            def observe(inner, *, target_id, incarnation_id, node_id):
                from django.db import connection
                self.assertFalse(connection.in_atomic_block)
                return self.p.FactV1(target_id, incarnation_id, 1, now, now, 'fresh', (('clean', inner.clean),))
        clone = Clone()
        assignments = assignments_service(clones=clone)
        values = dict(expected_revision=target.revision, role='worker', bundle_digest='b'*64,
            grant_id=uuid4(), expires_at=timezone.now()+timedelta(hours=1))
        with self.assertRaises(self.s.Conflict):
            assignments.reassign(self.owner, target.id, **values)
        clone.clean = True
        assigned = assignments.reassign(self.owner, target.id, **values)
        self.assertEqual(assigned.owner_id, self.human.id)
        self.assertGreater(assigned.generation, target.generation)
        with self.assertRaises(self.s.Conflict):
            assignments.release(self.owner, target.id, expected_revision=target.revision,
                expected_generation=target.generation)
        self.assertEqual(self.m.Assignment.objects.get(pk=target.id).owner_id, self.human.id)

    def test_clean_clone_observation_cannot_precede_old_effect_quiescence(self):
        attempt = self.claim()
        self.service.executor_restarted(self.actor, self.node.id, previous_epoch=self.boot, new_epoch=uuid4())
        revision = self.m.Assignment.objects.values_list('revision', flat=True).get(pk=self.target.id)
        assignments_service().release(self.owner, self.target.id, expected_revision=revision)
        target = self.m.Assignment.objects.get(pk=self.target.id)
        attempt = self.m.AssignmentAttempt.objects.get(pk=attempt.id)
        self.human.user_permissions.add(Permission.objects.get(content_type__app_label='execution', codename='reconcile_work'))
        class Inspector:
            def inspect(inner, work, attempt_id):
                return self.p.InspectionV1(attempt_id, work.incarnation_id, work.executor_epoch,
                    'succeeded', 'session-replaced', True)
        self.service.inspector = Inspector()
        class RacingClone:
            def observe(inner, *, target_id, incarnation_id, node_id):
                now = timezone.now()
                clean_before_effect_finished = self.p.FactV1(target_id, incarnation_id, 1,
                    now, now, 'fresh', (('clean', True),))
                # Эффект завершился и испортил clone после clean observation.
                self.service.reconcile(self.owner, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4())
                return clean_before_effect_finished
        with self.assertRaises(self.s.Conflict):
            assignments_service(clones=RacingClone()).reassign(self.owner, target.id,
                expected_revision=target.revision, role='worker', bundle_digest='b'*64,
                grant_id=uuid4(), expires_at=timezone.now()+timedelta(hours=1))
        self.assertIsNone(self.m.Assignment.objects.get(pk=target.id).owner_id)

    def test_activation_consumes_operation_reserved_incarnation_and_grant_values(self):
        reserved_incarnation, reserved_grant = uuid4(), uuid4()
        try:
            incarnation = assignments_service().activate(self.owner, self.target.id,
                expected_revision=self.target.revision, executor_epoch=self.boot,
                incarnation_id=reserved_incarnation, grant_id=reserved_grant)
        except TypeError:
            self.fail('activation must consume operation-reserved identity values, not invent another result identity')
        self.assertEqual(incarnation.id, reserved_incarnation)
        current = self.m.Assignment.objects.get(pk=self.target.id)
        self.assertEqual(current.grant_id, reserved_grant)
        self.assertEqual(current.owner_id, self.human.id)
        with self.assertRaises(self.s.Conflict):
            self.claim()

    def test_new_incarnation_cannot_replace_unresolved_effect(self):
        self.claim()
        with self.assertRaises(self.s.Conflict):
            assignments_service().activate(self.owner, self.target.id,
                expected_revision=self.target.revision, executor_epoch=uuid4())

    def test_node_restart_requires_reconciliation_not_old_claim_replay(self):
        attempt = self.claim()
        self.service.executor_restarted(self.actor, self.node.id, previous_epoch=self.boot, new_epoch=uuid4())
        attempt = self.m.AssignmentAttempt.objects.get(pk=attempt.id)
        self.assertEqual(attempt.state, 'unknown')
        with self.assertRaises(self.s.Conflict):
            self.claim(attempt.request_id)

    def test_restart_advances_assignment_cas_and_rejects_pre_restart_activation(self):
        old_revision = self.target.revision
        self.service.executor_restarted(self.actor, self.node.id,
            previous_epoch=self.boot, new_epoch=uuid4())
        current = self.m.Assignment.objects.get(pk=self.target.id)
        self.assertGreater(current.revision, old_revision)
        with self.assertRaises(self.s.Conflict):
            assignments_service().activate(self.owner, current.id,
                expected_revision=old_revision, executor_epoch=self.boot)
        self.assertEqual(self.m.Assignment.objects.get(pk=current.id).current_incarnation_id, self.inc.id)

    def test_restart_retry_after_activation_does_not_retire_new_incarnation(self):
        new_epoch = uuid4()
        self.service.executor_restarted(self.actor, self.node.id, previous_epoch=self.boot, new_epoch=new_epoch)
        current = self.m.Assignment.objects.get(pk=self.target.id)
        active = assignments_service().activate(self.owner, current.id,
            expected_revision=current.revision, executor_epoch=new_epoch)
        try:
            self.assertEqual(self.service.executor_restarted(self.actor, self.node.id,
                previous_epoch=self.boot, new_epoch=new_epoch), new_epoch)
        except self.s.Conflict:
            self.fail('applied restart notification must stay idempotent after the resulting incarnation is activated')
        self.assertTrue(self.m.ExecutionIncarnation.objects.get(pk=active.id).accepting)

    def test_revocation_and_grant_recheck_deny_before_admission(self):
        self.grants.allowed = False
        with self.assertRaises(self.s.Denied):
            self.claim()
        self.assertEqual(self.m.AssignmentAttempt.objects.count(), 0)
        self.grants.allowed = True
        self.auth.revoke_token(self.actor)
        with self.assertRaises(PermissionError):
            self.claim()

    def test_pinned_role_grant_owner_fence_and_deadline_cannot_be_forged(self):
        from dataclasses import replace
        changes = ({'role': 'other'}, {'bundle_digest': 'b'*64}, {'grant_id': uuid4()},
            {'owner_id': self.human.id+1}, {'generation': self.work.generation+1},
            {'revision': self.work.revision+1}, {'incarnation_id': uuid4()},
            {'executor_epoch': uuid4()}, {'not_after': timezone.now()-timedelta(seconds=1)})
        for change in changes:
            with self.subTest(change=tuple(change)):
                forged = replace(self.work, **change)
                self.port.work = forged
                with self.assertRaises(self.s.Conflict):
                    self.service.claim(self.actor, forged, request_id=uuid4())
        self.assertEqual(self.m.AssignmentAttempt.objects.count(), 0)
        self.assertEqual(self.port.started, 0)

    def test_forged_actor_and_foreign_node_are_denied(self):
        from dataclasses import replace
        with self.assertRaises(PermissionError):
            self.service.claim(replace(self.actor, user_id=self.human.id), self.work, request_id=uuid4())
        with self.assertRaises(self.s.Conflict):
            self.service.claim(self.actor, replace(self.work, node_id=uuid4()), request_id=uuid4())

    def test_late_rollback_does_not_revoke_new_human_assignment(self):
        old_generation = self.target.generation
        assignments_service().release(self.owner, self.target.id, expected_revision=self.target.revision)
        with self.assertRaises(self.s.Conflict):
            assignments_service().release(self.owner, self.target.id,
                expected_revision=self.target.revision, expected_generation=old_generation)
        self.target = self.m.Assignment.objects.get(pk=self.target.id)
        self.assertGreater(self.target.generation, old_generation)

    def test_result_and_revoke_race_cannot_restore_old_authority(self):
        attempt = self.claim()
        barrier = Barrier(2)
        def finish():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    self.service.result(self.actor, attempt.id, expected_revision=1,
                        receipt_id=uuid4(), outcome='succeeded', evidence='confirmed')
                    return 'confirmed'
                except self.s.Conflict:
                    return 'fenced'
            finally:
                close_old_connections()
        def revoke():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                assignments_service().release(self.owner, self.target.id, expected_revision=self.target.revision)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as workers:
            result = workers.submit(finish)
            revoked = workers.submit(revoke)
            self.assertIn(result.result(), ('confirmed', 'fenced'))
            revoked.result()
        self.target = self.m.Assignment.objects.get(pk=self.target.id)
        self.assertIsNone(self.target.owner_id)
        self.assertGreater(self.target.generation, self.work.generation)
        with self.assertRaises(self.s.Conflict):
            self.claim()

    def test_landing_ownership_does_not_grant_execution_or_terminal_control(self):
        landing = self.s.Landing().acquire(self.owner, self.project.id, expected_revision=0)
        self.assertEqual(landing.owner_id, self.human.id)
        with self.assertRaises(self.s.Conflict):
            self.s.Landing().acquire(self.owner, self.project.id, expected_revision=0)
        self.assertEqual(self.m.AssignmentAttempt.objects.count(), 0)
        with self.assertRaises(PermissionError):
            self.s.Landing().acquire(self.actor, self.project.id, expected_revision=landing.revision)
        self.s.Landing().release(self.owner, self.project.id, expected_revision=landing.revision)
        with self.assertRaises(self.s.Conflict):
            self.s.Landing().release(self.owner, self.project.id, expected_revision=landing.revision)

    def test_identity_receipt_is_bound_to_current_generation_before_send(self):
        from dataclasses import replace
        identity = replace(self.work, action='apply_identity')
        self.port.work = identity
        attempt = self.service.claim(self.actor, identity, request_id=uuid4())
        self.service.result(self.actor, attempt.id, expected_revision=1, receipt_id=uuid4(),
            outcome='succeeded', evidence='identity-applied')
        self.assertEqual(self.m.IdentityApplication.objects.count(), 1)
        self.work = replace(self.work, operation_id=uuid4(), step_id=uuid4(), action='send_message')
        self.port.work = self.work
        self.claim()

    def test_readiness_requires_confirmed_named_step(self):
        from dataclasses import replace
        self.work = replace(self.work, action='confirm_ready')
        self.port.work = self.work
        attempt = self.claim()
        self.service.result(self.actor, attempt.id, expected_revision=1, receipt_id=uuid4(),
            outcome='unknown', evidence='connection-lost')
        self.assertEqual(self.m.ReadinessReceipt.objects.count(), 0)

    def test_send_is_denied_until_identity_application_is_confirmed(self):
        from dataclasses import replace
        self.work = replace(self.work, action='send_message')
        self.port.work = self.work
        with self.assertRaises(self.s.Conflict):
            self.claim()
        self.assertEqual(self.port.started, 0)

    def test_default_ports_deny_even_with_valid_machine_auth(self):
        with self.assertRaises(self.s.Unavailable):
            machine_execution_service().poll(self.actor, self.node.id, executor_epoch=self.boot, limit=10)

    @override_settings(ALLOWED_HOSTS=['testserver'])
    def test_drf_routes_use_current_auth_and_reject_arbitrary_rpc(self):
        client = Client(HTTP_AUTHORIZATION='Token '+self.key)
        response = client.post('/api/v1/machine/work/claim', {'schema_version':1, 'shell':'id'},
                               content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 400)
        response = client.get('/api/v1/machine/work', {'node_id':str(self.node.id),
            'executor_epoch':str(self.boot), 'schema_version':1}, secure=True)
        self.assertEqual(response.status_code, 503)
        response = Client().get('/api/v1/machine/work', secure=True)
        self.assertIn(response.status_code, (401,403))

    def test_restart_recovery_requires_verified_quiescence_not_a_boolean_from_wire(self):
        attempt = self.claim()
        self.service.executor_restarted(self.actor, self.node.id, previous_epoch=self.boot, new_epoch=uuid4())
        attempt = self.m.AssignmentAttempt.objects.get(pk=attempt.id)
        self.assertTrue(callable(getattr(self.service, 'reconcile', None)),
                        'unknown attempt requires an explicit inspected reconciliation service')
        self.human.user_permissions.add(Permission.objects.get(content_type__app_label='execution', codename='reconcile_work'))
        with self.assertRaises(self.s.Unavailable):
            self.service.reconcile(self.owner, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4())
        class Inspector:
            quiescent = False
            def inspect(inner, work, attempt_id):
                from django.db import connection
                self.assertFalse(connection.in_atomic_block, 'external inspection must not hold DB transaction')
                return self.p.InspectionV1(attempt_id, work.incarnation_id, work.executor_epoch,
                    'succeeded', 'session-replaced', inner.quiescent)
        inspector = Inspector()
        self.service.inspector = inspector
        with self.assertRaises(self.s.Conflict):
            self.service.reconcile(self.owner, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4())
        inspector.quiescent = True
        receipt = self.service.reconcile(self.owner, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4())
        self.assertEqual(receipt.outcome, 'succeeded')
        self.assertEqual(self.m.AssignmentAttempt.objects.get(pk=attempt.id).state, 'succeeded')
        current = self.m.Assignment.objects.get(pk=self.target.id)
        assignments_service().activate(self.owner, current.id, expected_revision=current.revision, executor_epoch=uuid4())
        with self.assertRaises(self.s.Conflict):
            self.claim()

    def test_public_binding_query_resolves_live_canonical_scope_without_private_model_imports(self):
        assignments = assignments_service()
        self.assertTrue(callable(getattr(assignments, 'current', None)),
                        'Access/Secrets composition needs a public current assignment binding query')
        binding = assignments.current(project_id=self.project.id, target_id=self.target.id)
        self.assertEqual((binding.target_id, binding.node_id, binding.incarnation_id, binding.executor_epoch),
                         (self.target.id, self.node.id, self.inc.id, self.boot))
        self.assertEqual((binding.owner_id, binding.role, binding.bundle_digest, binding.grant_id),
                         (self.human.id, 'worker', 'a'*64, self.target.grant_id))
        self.assertFalse(binding.ready)
        self.assertFalse(binding.identity_applied)
        self.assertIsNone(assignments.current(project_id=uuid4(), target_id=self.target.id))
        assignments.release(self.owner, self.target.id, expected_revision=self.target.revision)
        self.assertIsNone(assignments.current(project_id=self.project.id, target_id=self.target.id))

    def test_historical_unknown_fence_can_be_reconciled_under_current_epoch_without_replay(self):
        attempt = self.claim()
        self.service.executor_restarted(self.actor, self.node.id, previous_epoch=self.boot, new_epoch=uuid4())
        attempt = self.m.AssignmentAttempt.objects.get(pk=attempt.id)
        self.human.user_permissions.add(Permission.objects.get(content_type__app_label='execution', codename='reconcile_work'))
        current_epoch = uuid4()
        self.service.installation = lambda: self.p.AuthorityV1(current_epoch, False)
        class Inspector:
            def inspect(inner, work, attempt_id):
                return self.p.InspectionV1(attempt_id, work.incarnation_id, work.executor_epoch,
                    'succeeded', 'session-replaced', True)
        self.service.inspector = Inspector()
        try:
            receipt = self.service.reconcile(self.owner, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4())
        except self.s.Conflict:
            self.fail('current-epoch authorized inspection must be able to close a historical unresolved fence')
        self.assertEqual(receipt.outcome, 'succeeded')
        self.assertEqual(self.m.AssignmentAttempt.objects.get(pk=attempt.id).epoch, self.epoch)
        self.service.installation = lambda: self.p.AuthorityV1(current_epoch, True)
        with self.assertRaises(self.s.Conflict):
            self.claim()

    def test_poll_project_scope_precedes_limit_so_authorized_work_is_not_starved(self):
        from dataclasses import replace
        from mop.server.catalog.models import Project, NodeProject
        foreign = Project.objects.create(name='foreign', origin='https://example.invalid/foreign.git')
        NodeProject.objects.create(node=self.node, project=foreign)
        target = self.m.Assignment.objects.create(project=foreign, node=self.node, owner=self.human,
            role='worker', bundle_digest='a'*64, grant_id=uuid4(), expires_at=timezone.now()+timedelta(hours=1))
        inc = self.m.ExecutionIncarnation.objects.create(assignment=target, node=self.node, executor_epoch=self.boot)
        target.current_incarnation = inc
        target.save(update_fields=['current_incarnation'])
        forbidden = replace(self.work, operation_id=uuid4(), step_id=uuid4(), target_id=target.id,
            incarnation_id=inc.id, grant_id=target.grant_id, generation=target.generation, revision=target.revision)
        class ScopedWork(FakeWork):
            def pending(inner, *, node_id, executor_epoch, limit, project_ids=None):
                rows = (forbidden, self.work) if project_ids is None else (self.work,)
                if project_ids is not None:
                    self.assertEqual(project_ids, (self.project.id,))
                return rows[:limit]
        self.service.work = ScopedWork(self.work)
        self.assertEqual(self.service.poll(self.actor, self.node.id, executor_epoch=self.boot, limit=1), (self.work,))

    def test_human_target_assignment_does_not_require_unrelated_projects_on_same_node(self):
        from mop.server.catalog.models import Project, NodeProject
        foreign = Project.objects.create(name='foreign', origin='https://example.invalid/foreign.git')
        NodeProject.objects.create(node=self.node, project=foreign)
        try:
            result = assignments_service().create(self.owner, project_id=self.project.id,
                node_id=self.node.id, role='worker', bundle_digest='a'*64, grant_id=uuid4(),
                expires_at=timezone.now()+timedelta(hours=1))
        except PermissionError:
            self.fail('own-project target assignment must not require unrelated node-project membership')
        self.assertEqual(result.project_id, self.project.id)

    def test_receipt_identity_race_on_different_resources_is_a_conflict(self):
        from dataclasses import replace
        target = assignments_service().create(self.owner, project_id=self.project.id,
            node_id=self.node.id, role='worker', bundle_digest='a'*64, grant_id=uuid4(),
            expires_at=timezone.now()+timedelta(hours=1))
        incarnation = assignments_service().activate(self.owner, target.id,
            expected_revision=1, executor_epoch=self.boot)
        target = self.m.Assignment.objects.get(pk=target.id)
        second_work = replace(self.work, operation_id=uuid4(), step_id=uuid4(), target_id=target.id,
            incarnation_id=incarnation.id, generation=target.generation, revision=target.revision, grant_id=target.grant_id)
        second = machine_execution_service(work=FakeWork(second_work), grants=self.grants,
            installation=lambda: self.p.AuthorityV1(self.epoch, True))
        attempts = (self.claim(), second.claim(self.actor, second_work, request_id=uuid4()))
        barrier, receipt_id = Barrier(2), uuid4()
        def worker(pair):
            service, attempt = pair
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    service.result(self.actor, attempt.id, expected_revision=1,
                        receipt_id=receipt_id, outcome='succeeded', evidence='session-replaced')
                    return 'accepted'
                except self.s.Conflict:
                    return 'conflict'
                except Exception as error:
                    return type(error).__name__
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(worker, zip((self.service, second), attempts)))
        self.assertEqual(sorted(outcomes), ['accepted', 'conflict'])
        self.assertEqual(self.m.ResultReceipt.objects.count(), 1)
        self.assertEqual(self.m.AssignmentAttempt.objects.filter(state='claimed').count(), 1)

    def test_fact_scope_is_rechecked_against_actual_target_project(self):
        from mop.server.access.models import ProjectMembership
        now = timezone.now()
        page = self.p.FactPageV1(self.node.id, 'states', True, (
            self.p.FactV1(self.target.id, self.inc.id, 1, now, now, 'fresh', (('running', True),)),))
        class Reader:
            def read(inner, **scope):
                return page
        self.service.reader = Reader()
        self.assertEqual(self.service.facts(self.actor, self.node.id, kind='states', limit=10), page)
        ProjectMembership.objects.filter(user_id=self.actor.user_id).delete()
        with self.assertRaises(PermissionError):
            self.service.facts(self.actor, self.node.id, kind='states', limit=10)
        self.assertEqual(self.m.AssignmentAttempt.objects.count(), 0)

    def test_old_epoch_result_cannot_mutate_restored_timeline(self):
        attempt = self.claim()
        self.service.installation = lambda: self.p.AuthorityV1(uuid4(), True)
        with self.assertRaises(self.s.Conflict):
            self.service.result(self.actor, attempt.id, expected_revision=1,
                receipt_id=uuid4(), outcome='succeeded', evidence='session-replaced')
        self.assertEqual(self.m.ResultReceipt.objects.count(), 0)

    @override_settings(ALLOWED_HOSTS=['testserver'])
    def test_non_scalar_wire_action_is_validation_error_not_server_error(self):
        from unittest.mock import patch
        client = Client(HTTP_AUTHORIZATION='Token '+self.key)
        work = self.p.work_to_wire(self.work)
        work['action'] = ['shell', 'id']
        with patch('mop.server.controller.machineviews.execution', self.service):
            response = client.post('/api/v1/machine/work/claim',
                {'schema_version': 1, 'request_id': str(uuid4()), 'work': work},
                content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.m.AssignmentAttempt.objects.count(), 0)

    def test_epoch_change_or_disabled_effects_cannot_reuse_admission(self):
        self.service = machine_execution_service(work=self.port, grants=self.grants,
            installation=lambda: self.p.AuthorityV1(uuid4(), True))
        with self.assertRaises(self.s.Conflict):
            self.claim()
        self.service = machine_execution_service(work=self.port, grants=self.grants,
            installation=lambda: self.p.AuthorityV1(self.epoch, False))
        with self.assertRaises(self.s.Unavailable):
            self.claim()
        self.assertEqual(self.m.AssignmentAttempt.objects.count(), 0)

    @override_settings(ALLOWED_HOSTS=['testserver'])
    def test_real_drf_claim_and_result_share_authenticated_service_and_receipt(self):
        from mop.server.controller.machineviews import authority
        self.service.installation = authority
        from unittest.mock import patch
        client = Client(HTTP_AUTHORIZATION='Token '+self.key)
        claim = {'schema_version': 1, 'request_id': str(uuid4()), 'work': self.p.work_to_wire(self.work)}
        with patch('mop.server.controller.machineviews.execution', self.service):
            response = client.post('/api/v1/machine/work/claim', claim,
                                   content_type='application/json', secure=True)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['state'], 'claimed')
            result = {'schema_version': 1, 'attempt_id': response.json()['attempt_id'],
                'expected_revision': response.json()['revision'], 'receipt_id': str(uuid4()),
                'outcome': 'succeeded', 'evidence': 'session-replaced'}
            first = client.post('/api/v1/machine/work/result', result, content_type='application/json', secure=True)
            retry = client.post('/api/v1/machine/work/result', result, content_type='application/json', secure=True)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json(), retry.json())
            human_client = Client(HTTP_AUTHORIZATION='Token '+self.auth.issue_token(self.human.id))
            denied = human_client.post('/api/v1/machine/work/claim', claim,
                                      content_type='application/json', secure=True)
            self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.port.started, 1)
        self.assertEqual(len(self.port.completed), 1)

    def test_fact_read_is_bounded_and_never_turns_unavailable_into_empty(self):
        with self.assertRaises(self.s.Unavailable):
            self.service.facts(self.actor, self.node.id, kind='disk', limit=10)
        with self.assertRaises(ValueError):
            self.service.facts(self.actor, self.node.id, kind='shell', limit=10)
        with self.assertRaises(ValueError):
            self.service.facts(self.actor, self.node.id, kind='states', limit=501)
        self.assertEqual(self.m.AssignmentAttempt.objects.count(), 0)


class FakeWork:
    """Тот же consumer port, без второй очереди или внешних effects."""
    def __init__(self, work):
        self.work, self.started, self.completed = work, 0, []

    def pending(self, *, node_id, executor_epoch, project_ids, limit):
        return (self.work,) if project_ids else ()

    def current(self, operation_id, step_id):
        return self.work

    def claimed(self, work, attempt_id):
        self.started += 1

    def completed_result(self, work, attempt_id, outcome, evidence):
        self.completed.append((attempt_id, outcome, evidence))


class FakeGrants:
    allowed = True

    def authorize(self, *, owner_id, project_id, target_id, grant_id, role, bundle_digest, incarnation_id, action):
        if not self.allowed:
            from mop.server.sessions.services import Denied
            raise Denied('grant revoked')
