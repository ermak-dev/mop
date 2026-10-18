"""Начальный create без циклов scheduler/identity (#721), настоящий PG17.

HYPOTHESIS: scheduler_register адресует ещё не подготовленный executable;
read_launch и producer требуют registered job раньше подготовки, а identity
требует живую сессию. Четыре старых шага не являются конечным launch-протоколом.
SPEC: prepare_body -> bootstrap_body -> initial_identity -> scheduler_register
-> native_exec -> confirm_ready -> activate_grant; отдельные tokens/receipts,
четыре retained claims первого prepare, без replay неизвестного эффекта.
SOLUTION: pinned SQL selection до provider I/O, finite purpose delivery и
planned admission без job; registered добавляет exact job/placement и producer ACK.
Phase leases включают cleanup; его обязательство переживает intent и unknown.
RESULT: graph, legacy-purpose bypass и потеря installed gate воспроизведены red;
30/30 phase checks, 160/160 create/producer/credential/schema PG17 и 179/179
смежных execution/identity checks green; девять pure/layer modules green.
STATUS: FIXED — see #721 (server choreography; physical ports закрыты).
Это reference node receipt, не запуск native CLI или физическая готовность.
"""
from uuid import UUID, uuid4

from django.test import TransactionTestCase, override_settings

from _create_cases import CreateVertical, ready_evidence


PHASES = ('prepare_body', 'bootstrap_body', 'initial_identity', 'scheduler_register',
          'native_exec', 'confirm_ready', 'activate_grant')


@override_settings(ALLOWED_HOSTS=['testserver'])
class InitialCreatePhases(TransactionTestCase):
    serialized_rollback = True
    setUp = CreateVertical.setUp
    ready, body, submit = CreateVertical.ready, CreateVertical.body, CreateVertical.submit

    def create(self):
        self.ready()
        response = self.submit(self.body())
        self.assertEqual(response.status_code, 202, response.content)
        from mop.server.operations.models import Operation
        return Operation.objects.get(pk=response.json()['operation']['id'])

    def machine(self):
        from mop.server.access import services as auth
        from mop.server.controller.createbinding import LaunchSupport
        from mop.server.controller.machinebinding import compose
        key = auth.enroll_service('initial-phase-reference', self.node.pk,
            project_ids=(self.project.pk,), permissions=('execution.poll_work', 'execution.claim_work',
                'execution.report_result', 'execution.read_launch', 'execution.register_producer'))
        self.machine_key = key
        return compose(grants=self.grants, launch_support=LaunchSupport()), auth.token_context(key)

    # HYPOTHESIS #730: после durable selection A credential reserve выбирает
    # latest B; независимые проверки допускают policy B рядом с launch A.
    # SPEC: настоящий protected socket должен закреплять approval/content/digest
    # A; отзыв A не заменяется B и отказывает до provision/creation intent.
    # SOLUTION: committed reservation хранит exact proof; protected command берёт
    # только его UUID и связывает обычный Grants.reserve с SQL approval query.
    # RESULT: оба socket/PG17 checks red (grant B рядом с A; grant после отзыва A),
    # затем 233 create/initial/credential/grant/schema checks и pure boundaries green.
    # STATUS: FIXED — see #730.
    def protected_submit(self, body, before_reserve):
        import asyncio
        import os
        import tempfile
        from pathlib import Path
        from asgiref.sync import async_to_sync
        from unittest.mock import patch
        from mop.common.credential_client import CredentialClient
        from mop.server.controller.credentials import CredentialService
        from mop.server.controller.runtime import Runtime
        app = self.credential_app()
        original = self.ops.creates._agent_credential
        def boundary(*args):
            original(*args)
            before_reserve()
        async def exercise(path):
            runtime = Runtime(database_limit=2, queue_limit=0)
            service = CredentialService(path, application=app, runtime=runtime,
                allowed_uids=(os.geteuid(),), timeout=5)
            await service.start()
            try:
                issuer = CredentialClient(path, server_uid=os.geteuid(), timeout=5)
                with patch.object(self.ops.creates, 'issuer', issuer), \
                        patch.object(self.ops.creates, '_agent_credential', side_effect=boundary):
                    def submit():
                        from django.db import connections
                        try:
                            return self.submit(body)
                        finally:
                            connections.close_all()
                    return await asyncio.to_thread(submit)
            finally:
                await service.aclose()
                await runtime.aclose()
        with tempfile.TemporaryDirectory(prefix='mop-initial-pin-') as directory:
            return async_to_sync(exercise)(str(Path(directory)/'credential.sock'))

    def later_bootstrap(self):
        revision = self.preparation.publish(self.actor, project_id=self.project.pk,
            tasks=b'- debug: msg=later\n', variables=b'later: true\n',
            provenance='concurrent-approved-fixture', commit='f'*40, secret_refs=())
        self.later_approval = self.preparation.approve(self.actor, revision.id)
        return revision

    def test_initial_reservation_pins_selected_approval_across_protected_service_race(self):
        from mop.server.access.models import Grant
        from mop.server.operations.models import Operation
        from mop.server.sessions.models import IncarnationLaunch
        self.ready()
        response = self.protected_submit(self.body(), self.later_bootstrap)
        self.assertEqual(response.status_code, 202, response.content)
        row = Operation.objects.get(pk=response.json()['operation']['id'])
        intent = Grant.objects.select_related('intent').get(pk=row.restartbinding.grant_id).intent
        self.assertEqual((intent.policy_approval_id, intent.policy_version_id, intent.policy_digest),
            (self.bootstrap_approval.id, self.revision.content_version_id, self.revision.digest))
        launch = IncarnationLaunch.objects.get(incarnation_id=row.incarnation_id)
        self.assertEqual((launch.bootstrap_revision, launch.bootstrap_digest), (self.revision.id, self.revision.digest))
        reservation = row.createbinding.reservation
        self.assertEqual((reservation.bootstrap_approval_id, reservation.bootstrap_version_id, reservation.bootstrap_digest),
            (intent.policy_approval_id, intent.policy_version_id, intent.policy_digest))
        # Отзыв невыбранного latest B не должен отнимать authority у A.
        self.preparation.revoke(self.actor, self.later_approval.id)
        execution, actor = self.machine()
        work, attempt = self.claim_next(execution, actor, row, 'prepare_body')
        from mop.common.credentials import Credential, Command
        from mop.server.access.services import Denied
        from mop.server.sessions.protocol import work_to_wire
        self.preparation.revoke(self.actor, self.bootstrap_approval.id)
        with self.assertRaisesRegex(Denied, 'revoked'):
            self.credential_app().execute(Credential('token', self.machine_key), Command.INITIAL_LAUNCH,
                dict(work=work_to_wire(work)))

    def test_revoked_selected_approval_is_not_replaced_by_latest_in_protected_reserve(self):
        from mop.server.access.models import Grant
        from mop.server.operations.models import Operation
        self.ready()
        def revoke():
            self.later_bootstrap()
            self.preparation.revoke(self.actor, self.bootstrap_approval.id)
        response = self.protected_submit(self.body(), revoke)
        self.assertNotEqual(response.status_code, 202, response.content)
        self.assertFalse(Grant.objects.exists(), 'revoked A must be refused before reserving or provisioning B')
        self.assertFalse(Operation.objects.exists())

    def test_admission_captures_the_fixed_initial_create_sequence(self):
        row = self.create()
        self.assertEqual(tuple(row.step_set.order_by('ordinal').values_list('name', flat=True)), PHASES)

    def test_first_planned_work_does_not_require_a_registered_job(self):
        row = self.create()
        from mop.server.operations.models import ScheduledPuppet
        self.assertFalse(ScheduledPuppet.objects.filter(target_id=row.target_id).exists())
        execution, actor = self.machine()
        work = execution.poll(actor, self.node.pk, executor_epoch=self.boot)
        self.assertEqual(tuple(item.action for item in work), ('prepare_body',))
        attempt = execution.claim(actor, work[0], request_id=uuid4())
        self.assertEqual(attempt.state, 'claimed')
        current, claim = execution.action_admission(actor, operation_id=row.id, step_id=work[0].step_id)
        self.assertEqual((current, claim), (work[0], attempt.id))

    def test_missing_trusted_sql_selection_is_unavailable_before_provisioning(self):
        self.ready()
        from mop.server.catalog import models as catalog
        if hasattr(catalog, 'InitialCreateSelection'):
            catalog.InitialCreateSelection.objects.all().delete()
        response = self.submit(self.body())
        self.assertEqual(response.status_code, 503, response.content)
        self.assertIn('selection', response.json()['error']['message'])
        self.assertEqual(self.issuer.calls, [])

    def test_production_has_a_real_sql_selection_reader(self):
        from mop.server.controller import createbinding
        self.assertTrue(hasattr(createbinding, 'LaunchSelections'),
            'production create composition needs a SQL launch selection reader, not test support')

    def claim_next(self, execution, actor, row, action):
        works = execution.poll(actor, self.node.pk, executor_epoch=self.boot)
        self.assertEqual(tuple(work.action for work in works), (action,))
        work = works[0]
        self.assertEqual(work.operation_id, row.id)
        return work, execution.claim(actor, work, request_id=uuid4())

    def planned(self, row, execution, actor):
        from mop.common.initialcreate import CONFIRMATIONS
        for action in PHASES[:3]:
            work, attempt = self.claim_next(execution, actor, row, action)
            execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
                outcome='succeeded', evidence=CONFIRMATIONS[action])
        return work

    def test_full_planned_receipts_discover_scheduler_then_registered_native(self):
        from _register_peer import RegisterPeer
        from mop.server.operations.scheduler import NomadStopScheduler
        from mop.server.operations.models import Operation, ScheduledPuppet
        peer = RegisterPeer()
        self.addCleanup(peer.close)
        scheduler = NomadStopScheduler(origin=peer.origin, scheduler_id=uuid4(), token='fixture-only', timeout=2)
        self.ready(scheduler=scheduler)
        row = Operation.objects.get(pk=self.submit(self.body()) .json()['operation']['id'])
        execution, actor = self.machine()
        self.planned(row, execution, actor)
        self.assertEqual(self.ops.pending(handlers=('scheduler_register',)), (row.id,))
        self.assertIsNotNone(self.ops.launches.run(row.id))
        self.assertEqual(len(peer.posts), 1)
        self.assertTrue(ScheduledPuppet.objects.filter(target_id=row.target_id).exists())
        self.claim_next(execution, actor, row, 'native_exec')

    def test_bootstrap_claim_has_its_full_effect_and_cleanup_lease(self):
        from datetime import timedelta
        from django.utils import timezone
        from mop.server.sessions.models import AssignmentAttempt
        row = self.create()
        execution, actor = self.machine()
        work, attempt = self.claim_next(execution, actor, row, 'prepare_body')
        execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
            outcome='succeeded', evidence='body-prepared')
        before = timezone.now()
        work, attempt = self.claim_next(execution, actor, row, 'bootstrap_body')
        claimed = AssignmentAttempt.objects.get(pk=attempt.id)
        self.assertGreaterEqual(claimed.lease_until, before+timedelta(seconds=320))
        self.assertLessEqual(claimed.lease_until, timezone.now()+timedelta(seconds=320))
        self.assertLessEqual(claimed.lease_until, work.not_after)

    # HYPOTHESIS #731: rollback недостаточного phase budget стирает abort, а
    # expired-attempt recovery не видит gap после quiescent prepare_body.
    # SPEC: definite abort durable без новой attempt; настоящие receipts/claims
    # неизменны, cleanup required, ни release по времени, ни replay/renewal.
    # SOLUTION: SQL-only abort commit до отказа claim; bounded frontier recovery
    # вызывается и обычным machine reconciler, и каждым controller worker poll.
    # RESULT: PG17 — четыре gap checks red, затем отдельный limit=1 starvation red;
    # 292 create/execution/worker/operation/replacement/build/schema checks green,
    # шесть pure contract/layer modules green. Physical cleanup не исполнялся.
    # STATUS: FIXED — see #731.
    def prepared_gap(self):
        row = self.create()
        execution, actor = self.machine()
        work, attempt = self.claim_next(execution, actor, row, 'prepare_body')
        execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
            outcome='succeeded', evidence='body-prepared')
        work, = execution.poll(actor, self.node.pk, executor_epoch=self.boot)
        self.assertEqual(work.action, 'bootstrap_body')
        return row, execution, actor, work

    def gap_evidence(self, row):
        from mop.server.operations import models as om
        from mop.server.sessions import models as sm
        return (tuple(row.step_set.order_by('ordinal').values()),
            tuple(om.ResourceClaim.objects.filter(operation=row).order_by('resource_id').values()),
            tuple(om.EffectReceipt.objects.filter(step__operation=row).order_by('pk').values()),
            tuple(sm.AssignmentAttempt.objects.filter(operation_id=row.id).order_by('pk').values()),
            tuple(sm.ResultReceipt.objects.filter(attempt__operation_id=row.id).order_by('pk').values()),
            om.CreateCleanup.objects.get(operation=row).armed_at)

    def assert_aborted_gap(self, row, execution, actor, evidence):
        from mop.server.operations import models as om
        row.refresh_from_db()
        self.assertEqual((row.state, row.code), ('failed', 'deadline_expired'))
        self.assertIsNotNone(row.completed_at)
        cleanup = om.CreateCleanup.objects.get(operation=row)
        self.assertIsNotNone(cleanup.required_at)
        self.assertIsNone(cleanup.confirmed_at)
        self.assertIsNone(cleanup.adopted_at)
        self.assertEqual(self.gap_evidence(row), evidence)
        self.assertEqual(om.ResourceClaim.objects.filter(operation=row, released_at=None).count(), 4)
        self.assertEqual(execution.poll(actor, self.node.pk, executor_epoch=self.boot), ())
        revision = row.revision
        execution.reconcile_expired()
        row.refresh_from_db()
        self.assertEqual(row.revision, revision)
        self.assertEqual(om.AuditEvent.objects.filter(operation=row, code='deadline_expired').count(), 1)
        self.assertEqual(om.OutboxEvent.objects.filter(operation=row, code='deadline_expired').count(), 1)

    def test_insufficient_between_phase_budget_commits_abort_without_an_attempt(self):
        from datetime import timedelta
        from unittest.mock import patch
        from mop.server.operations.service import Refused
        from mop.server.sessions.services import Conflict
        row, execution, actor, work = self.prepared_gap()
        evidence = self.gap_evidence(row)
        with patch('django.utils.timezone.now', return_value=row.not_after-timedelta(seconds=319)):
            with self.assertRaises((Refused, Conflict)):
                execution.claim(actor, work, request_id=uuid4())
            self.assert_aborted_gap(row, execution, actor, evidence)

    def test_expired_between_phase_gap_is_found_without_an_active_attempt(self):
        from datetime import timedelta
        from unittest.mock import patch
        row, execution, actor, work = self.prepared_gap()
        evidence = self.gap_evidence(row)
        with patch('django.utils.timezone.now', return_value=row.not_after+timedelta(seconds=1)):
            execution.reconcile_expired()
            self.assert_aborted_gap(row, execution, actor, evidence)

    def test_gap_recovery_filters_stale_installations_before_its_limit(self):
        # HYPOTHESIS: stale epoch/generation либо отсутствие predecessor
        # занимают весь batch, и настоящий gap снова никогда не обнаруживается.
        from datetime import timedelta
        from unittest.mock import patch
        from mop.server.operations import models as om
        row, execution, actor, work = self.prepared_gap()
        evidence = self.gap_evidence(row)
        source = om.Operation.objects.filter(pk=row.id).values().get()
        stale = []
        for field in ('epoch', 'restore_generation', 'unstarted'):
            values = {**source, 'id': uuid4(), 'request_id': uuid4(),
                'not_after': row.not_after-timedelta(seconds=10)}
            if field != 'unstarted':
                values[field] = uuid4()
            other = om.Operation.objects.create(**values)
            # Только исторические locators: не выдумываем receipts или cleanup.
            om.Step.objects.create(operation=other, name='prepare_body', ordinal=1,
                outcome='' if field == 'unstarted' else 'succeeded')
            om.Step.objects.create(operation=other, name='bootstrap_body', ordinal=2)
            stale.append(other)
        with patch('django.utils.timezone.now', return_value=row.not_after+timedelta(seconds=1)):
            execution.reconcile_expired(limit=1)
            self.assert_aborted_gap(row, execution, actor, evidence)
        self.assertEqual(om.Operation.objects.filter(pk__in=[item.pk for item in stale], state='running').count(), 3)

    def test_controller_worker_discovers_expired_machine_phase_gap(self):
        import asyncio
        from datetime import timedelta
        from unittest.mock import patch
        from asgiref.sync import async_to_sync
        from mop.server.controller.runtime import Runtime
        from mop.server.operations.worker import Worker
        row, execution, actor, work = self.prepared_gap()
        evidence = self.gap_evidence(row)
        async def recover():
            runtime = Runtime(database_limit=1, queue_limit=0)
            try:
                return await asyncio.wait_for(Worker(runtime, self.ops, None).run_pending(limit=2), 5)
            finally:
                await runtime.aclose()
        with patch('django.utils.timezone.now', return_value=row.not_after+timedelta(seconds=1)):
            self.assertEqual(async_to_sync(recover)(), [])
            self.assert_aborted_gap(row, execution, actor, evidence)

    def concurrent_gap_claim_and_recovery(self, execution, actor, work):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import connections
        from mop.server.operations.service import Refused
        from mop.server.sessions.services import Conflict
        barrier = Barrier(2)
        def run(claim):
            connections.close_all()
            try:
                barrier.wait(timeout=5)
                if claim:
                    try:
                        return execution.claim(actor, work, request_id=uuid4())
                    except (Refused, Conflict):
                        return None
                execution.reconcile_expired()
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed, recovered = tuple(pool.map(run, (True, False)))
        return claimed

    def test_concurrent_claim_and_gap_recovery_commit_only_one_abort(self):
        from datetime import timedelta
        from unittest.mock import patch
        row, execution, actor, work = self.prepared_gap()
        evidence = self.gap_evidence(row)
        with patch('django.utils.timezone.now', return_value=row.not_after-timedelta(seconds=319)):
            self.assertIsNone(self.concurrent_gap_claim_and_recovery(execution, actor, work))
            self.assert_aborted_gap(row, execution, actor, evidence)

    def test_concurrent_recovery_does_not_abort_a_fully_budgeted_claim(self):
        from datetime import timedelta
        from unittest.mock import patch
        from mop.server.operations import models as om
        from mop.server.sessions.models import AssignmentAttempt
        row, execution, actor, work = self.prepared_gap()
        held = tuple(om.ResourceClaim.objects.filter(operation=row).order_by('resource_id').values())
        now = row.not_after-timedelta(seconds=320)
        with patch('django.utils.timezone.now', return_value=now):
            attempt = self.concurrent_gap_claim_and_recovery(execution, actor, work)
            self.assertIsNotNone(attempt)
            execution.reconcile_expired()
        row.refresh_from_db()
        self.assertEqual(row.state, 'running')
        self.assertIsNone(om.CreateCleanup.objects.get(operation=row).required_at)
        self.assertEqual(tuple(om.ResourceClaim.objects.filter(operation=row).order_by('resource_id').values()), held)
        claimed = AssignmentAttempt.objects.get(pk=attempt.id)
        self.assertEqual(claimed.lease_until, now+timedelta(seconds=320))
        self.assertEqual(claimed.state, 'claimed')
        self.assertEqual(AssignmentAttempt.objects.filter(operation_id=row.id).count(), 2)

    def test_next_machine_claim_requires_all_retained_prepare_fences(self):
        from mop.server.operations.models import ResourceClaim
        from mop.server.operations.service import Refused
        from mop.server.access.services import Denied
        from mop.server.sessions.services import Conflict
        from django.utils import timezone
        row = self.create()
        execution, actor = self.machine()
        work, attempt = self.claim_next(execution, actor, row, 'prepare_body')
        execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
            outcome='succeeded', evidence='body-prepared')
        ResourceClaim.objects.filter(operation=row, resource_id='target:'+str(row.target_id)).update(released_at=timezone.now())
        work, = execution.poll(actor, self.node.pk, executor_epoch=self.boot)
        with self.assertRaises((Denied, Refused, Conflict)):
            execution.claim(actor, work, request_id=uuid4())

    def test_phase_delivery_has_a_separate_strict_purpose_port(self):
        from mop.common import credentials
        self.assertIn('read_initial_launch', tuple(command.value for command in credentials.Command))

    def registered(self):
        from _register_peer import RegisterPeer
        from mop.server.operations.scheduler import NomadStopScheduler
        from mop.server.operations.models import Operation
        peer = RegisterPeer()
        self.addCleanup(peer.close)
        scheduler = NomadStopScheduler(origin=peer.origin, scheduler_id=uuid4(), token='fixture-only', timeout=2)
        self.ready(scheduler=scheduler)
        response = self.submit(self.body())
        self.assertEqual(response.status_code, 202, response.content)
        row = Operation.objects.get(pk=response.json()['operation']['id'])
        execution, actor = self.machine()
        self.planned(row, execution, actor)
        self.ops.launches.run(row.id)
        return row, execution, actor

    def test_registered_admission_keeps_the_installed_capability_gate(self):
        # HYPOTHESIS #721: registered admission добавляет job proof, но теряет
        # исходный gate реально enrolled physical/anchor capabilities.
        from mop.server.operations.models import ScheduledNode
        from mop.server.operations.service import Refused
        from mop.server.access.services import Denied
        from mop.server.sessions.services import Conflict
        row, execution, actor = self.registered()
        ScheduledNode.objects.filter(node=self.node).update(capabilities=[], anchor_executable='', anchor_digest='')
        work, = execution.poll(actor, self.node.pk, executor_epoch=self.boot)
        with self.assertRaises((Refused, Denied, Conflict)):
            execution.claim(actor, work, request_id=uuid4())
        self.assertIsNone(row.step_set.get(name='native_exec').token)

    def test_native_success_requires_its_exact_registered_producer_ack(self):
        from mop.server.sessions.services import Conflict
        row, execution, actor = self.registered()
        work, attempt = self.claim_next(execution, actor, row, 'native_exec')
        with self.assertRaisesRegex(Conflict, 'producer'):
            execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
                outcome='succeeded', evidence='native-started')

    def native(self):
        from _create_cases import native_ack
        row, execution, actor = self.registered()
        work, attempt = self.claim_next(execution, actor, row, 'native_exec')
        scope = native_ack(self, work, self.machine_key)
        execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
            outcome='succeeded', evidence='native-started')
        return row, execution, actor, scope

    def test_readiness_requires_the_same_native_producer_not_merely_a_ready_receipt(self):
        from mop.server.sessions.producer_models import CurrentProducer
        from mop.server.sessions.services import Conflict
        row, execution, actor, scope = self.native()
        work, attempt = self.claim_next(execution, actor, row, 'confirm_ready')
        proof = ready_evidence(self, execution, actor, work, attempt, self.machine_key)
        CurrentProducer.objects.filter(incarnation_id=work.incarnation_id).delete()
        with self.assertRaisesRegex(Conflict, 'producer'):
            execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
                outcome='succeeded', evidence='ready', ready_evidence=proof)

    def test_reference_lifecycle_activates_only_after_all_seven_exact_phases(self):
        from mop.server.access import services as auth
        from mop.server.controller.credentials import CreateActivation
        from mop.server.sessions.producer_models import CurrentProducer
        from mop.server.operations import models as om
        row, execution, actor, scope = self.native()
        work, attempt = self.claim_next(execution, actor, row, 'confirm_ready')
        execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
            outcome='succeeded', evidence='ready',
            ready_evidence=ready_evidence(self, execution, actor, work, attempt, self.machine_key))
        key = auth.enroll_service('phase-activator', self.node.pk, project_ids=(self.project.pk,),
            permissions=('access.activate_grant',), use_case='grant-activator')
        from _create_cases import CreateActivation as Fixture
        self.ops.activations.activator = Fixture.activator(self)
        ticket = self.ops.activations.claim(row.id)
        self.assertIsNotNone(ticket)
        authority = CreateActivation(self.credential_app(), auth.token_context(key), ticket[2])
        authority.require()
        from _create_cases import CreateActivation as Fixture
        self.ops.activations.activator = Fixture.activator(self)
        receipt = self.ops.activations.activator.activate_grant(ticket[2], expected_revision=ticket[3])
        self.assertEqual(receipt.state, 'active')
        self.ops.activations.record(row.id, ticket[1], 'succeeded', 'grant-activated')
        row.refresh_from_db()
        self.assertEqual(row.state, 'succeeded')
        self.assertEqual(tuple(row.step_set.order_by('ordinal').values_list('outcome', flat=True)), ('succeeded',)*7)
        self.assertFalse(om.ResourceClaim.objects.filter(operation=row, released_at=None).exists())
        self.assertIsNotNone(om.CreateCleanup.objects.get(operation=row).adopted_at)
        self.assertEqual(CurrentProducer.objects.get(incarnation_id=work.incarnation_id).producer.producer_epoch, scope.producer_epoch)

    def test_scheduler_last_gate_rejects_a_released_retained_claim(self):
        from _register_peer import RegisterPeer
        from mop.server.operations.scheduler import NomadStopScheduler
        from mop.server.operations import models as om
        from django.utils import timezone
        peer = RegisterPeer()
        self.addCleanup(peer.close)
        self.ready(scheduler=NomadStopScheduler(origin=peer.origin, scheduler_id=uuid4(), token='fixture-only', timeout=2))
        row = om.Operation.objects.get(pk=self.submit(self.body()).json()['operation']['id'])
        execution, actor = self.machine()
        self.planned(row, execution, actor)
        ticket = self.ops.launches.claim(row.id)
        om.ResourceClaim.objects.filter(operation=row, resource_id='target:'+str(row.target_id)).update(released_at=timezone.now())
        self.assertFalse(self.ops.launches.prepare(ticket))
        self.assertEqual(peer.posts, [])

    def test_activation_never_recreates_a_missing_prepare_fence(self):
        from _create_cases import CreateActivation as Fixture
        from mop.server.operations import models as om
        from django.utils import timezone
        row, execution, actor, scope = self.native()
        work, attempt = self.claim_next(execution, actor, row, 'confirm_ready')
        execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
            outcome='succeeded', evidence='ready',
            ready_evidence=ready_evidence(self, execution, actor, work, attempt, self.machine_key))
        self.ops.activations.activator = Fixture.activator(self)
        om.ResourceClaim.objects.filter(operation=row, resource_id='target:'+str(row.target_id)).update(released_at=timezone.now())
        self.assertIsNone(self.ops.activations.claim(row.id))
        self.assertEqual(om.ResourceClaim.objects.filter(operation=row, released_at=None).count(), 3)

    def test_activation_needs_its_effect_plus_cleanup_remaining(self):
        from unittest.mock import patch
        from datetime import timedelta
        from _create_cases import CreateActivation as Fixture
        row, execution, actor, scope = self.native()
        work, attempt = self.claim_next(execution, actor, row, 'confirm_ready')
        execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
            outcome='succeeded', evidence='ready',
            ready_evidence=ready_evidence(self, execution, actor, work, attempt, self.machine_key))
        self.ops.activations.activator = Fixture.activator(self)
        with patch('mop.server.operations.launch.timezone.now', return_value=row.not_after-timedelta(seconds=29)):
            self.assertIsNone(self.ops.activations.claim(row.id))
        self.assertEqual(self.ops.activations.activator.calls, 0)

    def test_failed_preparation_keeps_cleanup_obligation_and_fences(self):
        from mop.server.operations import models as om
        row = self.create()
        execution, actor = self.machine()
        work, attempt = self.claim_next(execution, actor, row, 'prepare_body')
        execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
            outcome='failed', evidence='confirmed')
        self.assertTrue(hasattr(om, 'CreateCleanup'), 'cleanup requires a durable obligation, not a live intent')
        cleanup = om.CreateCleanup.objects.get(operation=row)
        self.assertIsNotNone(cleanup.required_at)
        self.assertIsNone(cleanup.confirmed_at)
        self.assertEqual(om.ResourceClaim.objects.filter(operation=row, released_at=None).count(), 4)

    def test_initial_delivery_parser_rejects_extensions_and_cross_phase_payload(self):
        from mop.common import initialcreate
        parser = getattr(initialcreate, 'parse_delivery', None)
        self.assertIsNotNone(parser, 'new delivery needs its own strict finite parser')
        from mop.server.controller.launchdelivery import deliver_initial
        from mop.server.sessions.protocol import work_to_wire
        row = self.create()
        execution, actor = self.machine()
        work, attempt = self.claim_next(execution, actor, row, 'prepare_body')
        wire = work_to_wire(work)
        value = deliver_initial(self.credential_app(), actor, wire)
        self.assertEqual(parser(value, wire), value)
        for changed in ({**value, 'secret_id': str(uuid4())}, {**value, 'phase': 'native_exec'},
                {**value, 'role': {'manifest': {}, 'prompt': ''}}, {**value, 'selection': {**value['selection'], 'secret_name': 'oracle'}}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                parser(changed, wire)

    def credential_app(self, provider=None):
        from mop.server.controller.credentials import CredentialApplication
        from mop.server.controller.models import Installation
        from mop.server.controller.runtime import require_restore_ready
        row = Installation.objects.get(pk=1)
        return CredentialApplication(provider or self.grants.secrets.provider,
            dict(identity=row.identity, epoch=str(row.epoch), root_digest=row.root_digest, effects_enabled=row.effects_enabled),
            require_restore_ready())

    def test_planned_delivery_selects_source_before_scheduler_without_owner_impersonation(self):
        from mop.server.controller import launchdelivery
        from mop.server.sessions.protocol import work_to_wire
        row = self.create()
        execution, actor = self.machine()
        work, attempt = self.claim_next(execution, actor, row, 'prepare_body')
        deliver = getattr(launchdelivery, 'deliver_initial', None)
        self.assertIsNotNone(deliver, 'purpose-specific initial launch delivery is missing')
        payload = deliver(self.credential_app(), actor, work_to_wire(work))
        # HYPOTHESIS #738: prepare не имеет ни job, ни локального name/default
        # policy; layout metadata обязаны уже приходить из immutable admission.
        # SOLUTION: тот же launch proof выдаёт assignment name и image.policy.
        # RESULT: RED — name отсутствовал; GREEN — 171/171 PostgreSQL checks.
        # STATUS: FIXED — see #738.
        from mop.server.sessions.models import Assignment
        from mop.server.catalog.models import ImageRevision
        self.assertEqual(payload.get('display_name'), Assignment.objects.get(pk=row.target_id).display_name)
        self.assertEqual(payload.get('image_revision'), str(self.image.id))
        policy = ImageRevision.objects.select_related('policy').get(pk=self.image.id).policy
        self.assertEqual(payload.get('image_policy'), dict(policy_id=str(policy.pk),
            cpu=policy.cpu, memory_mb=policy.memory_mb, disk_mb=policy.disk_mb))
        self.assertEqual(payload['phase'], 'prepare_body')
        self.assertEqual(payload['selection']['source'], dict(origin=self.project.origin, commit='d'*40, ref='refs/heads/exact-source'))
        self.assertIsNone(payload['bootstrap'])
        self.assertIsNone(payload['role'])
        self.assertEqual(payload['consumers'], [])
        self.assertEqual(payload['work'], work_to_wire(work))

    def test_legacy_launch_port_cannot_bypass_planned_phase_purpose(self):
        # HYPOTHESIS #721: расширение launch_definition на planned открывает
        # старый role-only endpoint до его native purpose.
        from mop.server.controller.launchdelivery import deliver
        from mop.server.sessions.protocol import work_to_wire
        from mop.server.access.services import Denied
        from mop.common.credentials import CredentialRefused
        from mop.common.initialcreate import CONFIRMATIONS
        from unittest.mock import patch
        row = self.create()
        execution, actor = self.machine()
        app = self.credential_app()
        for phase in PHASES[:3]:
            work, attempt = self.claim_next(execution, actor, row, phase)
            with patch.object(app.grants, 'read_launch_definition', wraps=app.grants.read_launch_definition) as reader:
                with self.subTest(phase=phase), self.assertRaises((Denied, CredentialRefused)):
                    deliver(app, actor, work_to_wire(work))
                reader.assert_not_called()
            execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
                outcome='succeeded', evidence=CONFIRMATIONS[phase])

    def test_approved_bootstrap_bytes_are_delivered_only_to_bootstrap_phase(self):
        from mop.server.controller import launchdelivery
        from mop.server.sessions.protocol import work_to_wire
        from mop.common import credentials as p
        row = self.create()
        execution, actor = self.machine()
        work, attempt = self.claim_next(execution, actor, row, 'prepare_body')
        execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
            outcome='succeeded', evidence='body-prepared')
        work, attempt = self.claim_next(execution, actor, row, 'bootstrap_body')
        deliver = getattr(launchdelivery, 'deliver_initial', None)
        self.assertIsNotNone(deliver, 'purpose-specific bootstrap delivery is missing')
        payload = deliver(self.credential_app(), actor, work_to_wire(work))
        self.assertEqual(p.binary(payload['bootstrap']['tasks']), b'- debug: msg=hello\n')
        self.assertEqual(p.binary(payload['bootstrap']['variables']), b'answer: 42\n')
        self.assertIsNone(payload['role'])
        self.assertEqual(payload['consumers'], [])

    def test_each_phase_delivers_only_selected_purpose_bytes_and_keeps_desired_model_unconfirmed(self):
        from _register_peer import RegisterPeer
        from _create_cases import native_ack
        from mop.server.operations.scheduler import NomadStopScheduler
        from mop.server.operations.models import Operation
        from mop.server.secrets.services import SecretStore
        from mop.server.access.services import Access
        from mop.server.controller.initialselection import configure
        from mop.server.controller.launchdelivery import deliver_initial
        from mop.server.sessions.protocol import work_to_wire
        from mop.common.initialcreate import parse_delivery, CONFIRMATIONS
        from mop.common import credentials as p
        peer = RegisterPeer()
        self.addCleanup(peer.close)
        self.ready(scheduler=NomadStopScheduler(origin=peer.origin, scheduler_id=uuid4(), token='fixture-only', timeout=2))
        store = SecretStore(provider=self.grants.secrets.provider, authorizer=Access())
        source = store.create(self.actor, project_id=self.project.pk, name='source-key',
            kind='credential', purpose='git', value=b'selected-source-private')
        model = store.create(self.actor, project_id=self.project.pk, name='model-key',
            kind='credential', purpose='proxy', value=b'selected-model-private')
        boot = store.create(self.actor, project_id=self.project.pk, name='BOOTSTRAP_VAR',
            kind='variable', purpose='project', value=b'selected-bootstrap-private')
        revision = self.preparation.publish(self.actor, project_id=self.project.pk,
            tasks=b'- debug: msg=approved\n', variables=b'approved: true\n', provenance='exact-fixture',
            commit='e'*40, secret_refs=(boot.active_version_id,))
        self.preparation.approve(self.actor, revision.id)
        configure(self.actor, project_id=self.project.pk, definition_id=self.definition.id,
            rbac_profile_id=self.selection.rbac_profile_id,
            bootstrap_revision=revision.id, image_id=self.image.id, source_origin=self.selection.source_origin,
            source_commit=self.selection.source_commit, source_ref=self.selection.source_ref,
            desired_model=self.selection.desired_model, model_config_digest=self.selection.model_config_digest,
            consumers=(('model_config', self.model_config.active_version_id), ('source_credential', source.active_version_id),
                       ('bootstrap_input', boot.active_version_id), ('model_credential', model.active_version_id)))
        row = Operation.objects.get(pk=self.submit(self.body()).json()['operation']['id'])
        execution, actor = self.machine()
        expected = {'prepare_body': [('source_credential', b'selected-source-private')],
            'bootstrap_body': [('bootstrap_input', b'selected-bootstrap-private')], 'initial_identity': [],
            'native_exec': [('model_config', b'{"model":"fixture-model"}'), ('model_credential', b'selected-model-private')],
            'confirm_ready': []}
        for phase in ('prepare_body', 'bootstrap_body', 'initial_identity', 'native_exec', 'confirm_ready'):
            if phase == 'native_exec':
                self.assertIsNotNone(self.ops.launches.run(row.id))
            work, attempt = self.claim_next(execution, actor, row, phase)
            wire = work_to_wire(work)
            payload = parse_delivery(deliver_initial(self.credential_app(), actor, wire), wire)
            self.assertEqual([(item['purpose'], p.binary(item['value'])) for item in payload['consumers']], expected[phase])
            self.assertEqual(payload['selection']['desired_model'], 'fixture-model')
            self.assertNotIn('confirmed_model', payload)
            self.assertEqual(payload['role'] is not None, phase in ('initial_identity', 'native_exec'))
            if payload['role'] is not None:
                self.assertEqual(p.binary(payload['role']['prompt']), b'fixture role')
            self.assertEqual(payload['bootstrap'] is not None, phase == 'bootstrap_body')
            if phase == 'native_exec':
                native_ack(self, work, self.machine_key)
            execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
                outcome='succeeded', evidence='ready' if phase == 'confirm_ready' else CONFIRMATIONS[phase],
                ready_evidence=(ready_evidence(self, execution, actor, work, attempt, self.machine_key)
                    if phase == 'confirm_ready' else None))

    def test_unknown_preparation_keeps_cleanup_and_never_replays_even_after_late_confirmation(self):
        from datetime import timedelta
        from unittest.mock import patch
        from mop.server.operations import models as om
        row = self.create()
        execution, actor = self.machine()
        work, attempt = self.claim_next(execution, actor, row, 'prepare_body')
        receipt = execution.result(actor, attempt.id, expected_revision=attempt.revision, receipt_id=uuid4(),
            outcome='unknown', evidence='connection-lost')
        self.assertEqual(execution.poll(actor, self.node.pk, executor_epoch=self.boot), ())
        self.assertIsNotNone(om.CreateCleanup.objects.get(operation=row).required_at)
        with patch('mop.server.sessions.services.timezone.now', return_value=row.not_after+timedelta(seconds=1)):
            execution.result(actor, attempt.id, expected_revision=receipt.revision, receipt_id=uuid4(),
                outcome='succeeded', evidence='body-prepared')
        self.assertEqual(execution.poll(actor, self.node.pk, executor_epoch=self.boot), ())
        row.refresh_from_db()
        self.assertEqual(row.state, 'unknown')
        self.assertEqual(om.ResourceClaim.objects.filter(operation=row, released_at=None).count(), 4)
        self.assertIsNone(om.CreateCleanup.objects.get(operation=row).confirmed_at)

    def test_concurrent_planned_claims_commit_one_attempt_and_one_set_of_fences(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import connections
        from mop.server.sessions.services import Conflict
        from mop.server.operations.service import Refused
        from mop.server.access.services import Denied
        from mop.server.operations import models as om
        from mop.server.sessions.models import AssignmentAttempt
        row = self.create()
        execution, actor = self.machine()
        work, = execution.poll(actor, self.node.pk, executor_epoch=self.boot)
        barrier = Barrier(2)
        def claim():
            try:
                barrier.wait(timeout=5)
                return execution.claim(actor, work, request_id=uuid4()).id
            except (Conflict, Refused, Denied):
                return None
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = tuple(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sum(value is not None for value in outcomes), 1)
        self.assertEqual(AssignmentAttempt.objects.filter(operation_id=row.id).count(), 1)
        claims = om.ResourceClaim.objects.filter(operation=row, released_at=None)
        self.assertEqual(claims.count(), 4)
        self.assertEqual(set(claims.values_list('token', flat=True)), set(outcomes)-{None})

    def test_lost_reservation_retry_keeps_its_policy_when_configured_selection_changes(self):
        from mop.server.controller.initialselection import configure
        from mop.server.operations.models import Operation, CreateReservation
        self.ready()
        body = self.body()
        first = self.selection
        CreateVertical.lose_reservation_reply(self, body)
        later = configure(self.actor, project_id=self.project.pk, definition_id=self.definition.id,
            bootstrap_revision=self.revision.id, image_id=self.image.id, source_origin=self.project.origin,
            source_commit='f'*40, source_ref='refs/heads/later', desired_model='later-model',
            model_config_digest=first.model_config_digest, consumers=first.consumers,
            rbac_profile_id=first.rbac_profile_id)
        self.assertNotEqual(first.policy_id, later.policy_id)
        response = self.submit(body)
        self.assertEqual(response.status_code, 202, response.content)
        row = Operation.objects.get(pk=response.json()['operation']['id'])
        self.assertEqual(row.createbinding.reservation.policy_id, first.policy_id)
        self.assertEqual(CreateReservation.objects.get(request_id=body['request_id']).policy_digest, first.digest)
        execution, actor = self.machine()
        work, attempt = self.claim_next(execution, actor, row, 'prepare_body')
        from mop.server.controller.launchdelivery import deliver_initial
        from mop.server.sessions.protocol import work_to_wire
        payload = deliver_initial(self.credential_app(), actor, work_to_wire(work))
        self.assertEqual(payload['selection']['source']['commit'], 'd'*40)

    def test_expired_preparation_lease_keeps_cleanup_and_all_fences_without_renewal(self):
        from unittest.mock import patch
        from datetime import timedelta
        from mop.server.operations import models as om
        row = self.create()
        execution, actor = self.machine()
        work, attempt = self.claim_next(execution, actor, row, 'prepare_body')
        with patch('mop.server.sessions.services.timezone.now', return_value=attempt.lease_until+timedelta(seconds=1)):
            execution.reconcile_expired()
        row.refresh_from_db()
        self.assertEqual(row.state, 'unknown')
        self.assertEqual(execution.poll(actor, self.node.pk, executor_epoch=self.boot), ())
        self.assertIsNotNone(om.CreateCleanup.objects.get(operation=row).required_at)
        self.assertEqual(om.ResourceClaim.objects.filter(operation=row, released_at=None).count(), 4)
        self.assertEqual(row.step_set.get(name='prepare_body').token, attempt.id)

    def test_revoked_selected_model_blocks_planned_admission_not_only_model_delivery(self):
        from mop.server.access.services import Denied
        from mop.server.operations.service import Refused
        from mop.server.sessions.services import Conflict
        row = self.create()
        execution, actor = self.machine()
        work, = execution.poll(actor, self.node.pk, executor_epoch=self.boot)
        self.credential_app().store.revoke(self.actor, self.model_config.id,
            expected_revision=self.model_config.revision)
        with self.assertRaises((Denied, Refused, Conflict)):
            execution.claim(actor, work, request_id=uuid4())
        self.assertIsNone(row.step_set.get(name='prepare_body').token)

    def test_first_prepare_cannot_adopt_partial_or_unproved_own_fences(self):
        from mop.server.operations import models as om
        from mop.server.access.services import Denied
        from mop.server.operations.service import Refused
        from mop.server.sessions.services import Conflict
        row = self.create()
        execution, actor = self.machine()
        first = om.Step.objects.get(operation=row, name='prepare_body')
        key = 'target:'+str(row.target_id)
        om.Resource.objects.get_or_create(key=key)
        om.ResourceClaim.objects.create(resource_id=key, operation=row, step=first, token=uuid4())
        work, = execution.poll(actor, self.node.pk, executor_epoch=self.boot)
        with self.assertRaises((Denied, Refused, Conflict)):
            execution.claim(actor, work, request_id=uuid4())
        first.refresh_from_db()
        self.assertIsNone(first.token)

    def test_scheduler_cannot_dispatch_after_its_nonrenewable_lease(self):
        from unittest.mock import patch
        from datetime import timedelta
        from _register_peer import RegisterPeer
        from mop.server.operations.scheduler import NomadStopScheduler
        from mop.server.operations import models as om
        peer = RegisterPeer()
        self.addCleanup(peer.close)
        self.ready(scheduler=NomadStopScheduler(origin=peer.origin, scheduler_id=uuid4(), token='fixture-only', timeout=2))
        row = om.Operation.objects.get(pk=self.submit(self.body()).json()['operation']['id'])
        execution, actor = self.machine()
        self.planned(row, execution, actor)
        ticket = self.ops.launches.claim(row.id)
        step = om.Step.objects.get(operation=row, name='scheduler_register')
        with patch('mop.server.operations.launch.timezone.now', return_value=step.lease_until+timedelta(seconds=1)):
            self.assertFalse(self.ops.launches.prepare(ticket))
        self.assertEqual(peer.posts, [])
        self.assertEqual(om.ResourceClaim.objects.filter(operation=row, released_at=None).count(), 4)
        self.assertIsNotNone(om.CreateCleanup.objects.get(operation=row).required_at)

    def test_missing_installed_anchor_is_unavailable_before_provisioning(self):
        from mop.server.operations.models import ScheduledNode
        self.ready()
        ScheduledNode.objects.filter(node=self.node).update(capabilities=[], anchor_executable='', anchor_digest='')
        response = self.submit(self.body())
        self.assertEqual(response.status_code, 503, response.content)
        self.assertIn('anchor', response.json()['error']['message'])
        self.assertEqual(self.issuer.calls, [])

    def test_scheduler_cannot_claim_before_planned_node_receipts(self):
        row = self.create()
        self.assertIsNone(self.ops.launches.claim(row.id),
            'scheduler must not launch a missing body before preparation and initial identity')
        from mop.server.operations.models import Step
        step = Step.objects.get(operation=row, name='scheduler_register')
        self.assertIsNone(step.token)
