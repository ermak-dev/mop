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
# Проверка app — assertion отсутствующей возможности, не import/setup failure.
class MachineExecution(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        self.assertTrue(apps.is_installed('mop.server.execution'),
                        'machine execution schema and services must be installed')
        from mop.server.execution import models, services, protocol
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
        self.target = services.Assignments().create(self.owner, project_id=self.project.pk,
            node_id=self.node.pk, role='worker', bundle_digest='a'*64, grant_id=uuid4(),
            expires_at=timezone.now()+timedelta(hours=1))
        self.inc = services.Assignments().activate(self.owner, self.target.id,
            expected_revision=1, executor_epoch=self.boot)
        self.target.refresh_from_db()
        self.work = protocol.WorkV1(epoch=self.epoch, owner_id=self.human.id,
            operation_id=uuid4(), step_id=uuid4(), target_id=self.target.id,
            incarnation_id=self.inc.id, node_id=self.node.id, executor_epoch=self.boot,
            generation=self.target.generation, revision=self.target.revision,
            role='worker', bundle_digest='a'*64, grant_id=self.target.grant_id,
            action='restart_session', not_after=timezone.now()+timedelta(minutes=2))
        self.port = FakeWork(self.work)
        self.grants = FakeGrants()
        self.service = services.MachineExecution(work=self.port, grants=self.grants)

    def claim(self, request_id=None):
        return self.service.claim(self.actor, self.work, request_id=request_id or uuid4())

    def test_claim_is_durable_and_receipt_retry_is_idempotent(self):
        request = uuid4()
        attempt = self.claim(request)
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

    def test_timeout_holds_conflicting_resource_and_never_replays(self):
        attempt = self.claim()
        self.m.AssignmentAttempt.objects.filter(pk=attempt.id).update(lease_until=timezone.now()-timedelta(seconds=1))
        self.service.reconcile_expired(limit=10)
        attempt = self.m.AssignmentAttempt.objects.get(pk=attempt.id)
        self.assertEqual(attempt.state, 'unknown')
        with self.assertRaises(self.s.Conflict):
            self.claim()
        self.assertEqual(self.port.started, 1)

    def test_new_incarnation_cannot_replace_unresolved_effect(self):
        self.claim()
        with self.assertRaises(self.s.Conflict):
            self.s.Assignments().activate(self.owner, self.target.id,
                expected_revision=self.target.revision, executor_epoch=uuid4())

    def test_node_restart_requires_reconciliation_not_old_claim_replay(self):
        attempt = self.claim()
        self.service.executor_restarted(self.actor, self.node.id, previous_epoch=self.boot, new_epoch=uuid4())
        attempt = self.m.AssignmentAttempt.objects.get(pk=attempt.id)
        self.assertEqual(attempt.state, 'unknown')
        with self.assertRaises(self.s.Conflict):
            self.claim(attempt.request_id)

    def test_revocation_and_grant_recheck_deny_before_admission(self):
        self.grants.allowed = False
        with self.assertRaises(self.s.Denied):
            self.claim()
        self.assertEqual(self.m.AssignmentAttempt.objects.count(), 0)
        self.grants.allowed = True
        self.auth.revoke_token(self.actor)
        with self.assertRaises(PermissionError):
            self.claim()

    def test_forged_actor_and_foreign_node_are_denied(self):
        from dataclasses import replace
        with self.assertRaises(PermissionError):
            self.service.claim(replace(self.actor, user_id=self.human.id), self.work, request_id=uuid4())
        with self.assertRaises(self.s.Conflict):
            self.service.claim(self.actor, replace(self.work, node_id=uuid4()), request_id=uuid4())

    def test_late_rollback_does_not_revoke_new_human_assignment(self):
        old_generation = self.target.generation
        self.s.Assignments().release(self.owner, self.target.id, expected_revision=self.target.revision)
        with self.assertRaises(self.s.Conflict):
            self.s.Assignments().release(self.owner, self.target.id,
                expected_revision=self.target.revision, expected_generation=old_generation)
        self.target.refresh_from_db()
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
                self.s.Assignments().release(self.owner, self.target.id, expected_revision=self.target.revision)
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as workers:
            result = workers.submit(finish)
            revoked = workers.submit(revoke)
            self.assertIn(result.result(), ('confirmed', 'fenced'))
            revoked.result()
        self.target.refresh_from_db()
        self.assertIsNone(self.target.owner_id)
        self.assertGreater(self.target.generation, self.work.generation)
        with self.assertRaises(self.s.Conflict):
            self.claim()

    def test_landing_ownership_does_not_grant_execution_or_terminal_control(self):
        landing = self.s.Landing().acquire(self.owner, self.project.id, expected_revision=0)
        self.assertEqual(landing.owner_id, self.human.id)
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
            self.s.MachineExecution().poll(self.actor, self.node.id, executor_epoch=self.boot, limit=10)

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

    def pending(self, *, node_id, executor_epoch, limit):
        return (self.work,)

    def current(self, operation_id, step_id):
        return self.work

    def claimed(self, work, attempt_id):
        self.started += 1

    def completed_result(self, work, attempt_id, outcome, evidence):
        self.completed.append((attempt_id, outcome, evidence))


class FakeGrants:
    allowed = True

    def authorize(self, *, owner_id, project_id, grant_id, role, bundle_digest, incarnation_id):
        if not self.allowed:
            from mop.server.execution.services import Denied
            raise Denied('grant revoked')
