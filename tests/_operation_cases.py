"""Поведенческий durable vertical #625 на независимых PostgreSQL connections."""
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connections, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone


from _operation_process import read as _read_in_process, claim as _claim_in_process


# FEATURE: durable admission, claim, receipt и REST result — один владелец.
# HYPOTHESIS: pure policies не переживают процесс и не защищают внешний эффект.
# SOLUTION: короткие ORM transactions, typed node handler и Runtime-owned worker.
# RED: PG16/17 и CI 5486/29528 на 273655c1 — отсутствует durable ORM.
# RED: отдельные PG checks — receipt CAS, restore 503, manual transaction,
#      restart inspection и Runtime shutdown без потери result.
# RED: PG16/17 — effect после expiry/revoke между claim и dispatch; новый
#      catalog revision блокировал подтверждённый receipt и quiescence.
# SOLUTION: отдельный dispatch checkpoint; immutable source receipt не зависит
#           от возможности обновить более новую catalog projection.
# RESULT: реальный PG подтверждает независимые процессы, атомарность и fences;
#         общий CI gate также включает отдельно исправляемые foundation fixtures.
# STATUS: FIXED — see #625
@override_settings(ALLOWED_HOSTS=['testserver'])
class DurableOperations(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        self.assertTrue(apps.is_installed('mop.server.operations'), 'durable Operations ORM must be installed')
        from mop.server.operations import models, service
        from mop.server.access import models as access, services
        from mop.server.catalog.models import Project, Node, NodeProject
        from mop.server.controller.runtime import initialize, operations_service
        from mop.server.controller.models import Installation, RestoreGuard
        # flush не повторяет data migration; fixture явно восстанавливает seed.
        RestoreGuard.objects.get_or_create(pk=1)
        initialize()
        Installation.objects.update(effects_enabled=True)
        self.epoch = Installation.objects.get().epoch
        self.m, self.s = models, service
        self.project = Project.objects.create(name='owned', origin='https://example.invalid/owned.git')
        self.node = Node.objects.create(name='owned', driver='host', ssh_host='example.invalid',
            control_origin='https://example.invalid', lan_address='example.invalid', bootstrap_address='example.invalid')
        NodeProject.objects.create(node=self.node, project=self.project)
        self.user = get_user_model().objects.create_user('operator', password='test-only')
        source = access.IdentitySource.objects.create(name='local', kind='local')
        access.Principal.objects.create(user=self.user, kind='human')
        access.LoginIdentity.objects.create(user=self.user, source=source, external_subject='operator')
        access.ProjectMembership.objects.create(user=self.user, project=self.project, source=source)
        access.AllProjectMembership.objects.create(user=self.user, source=source)
        for app, actions in (('catalog', ('drain_node', 'enable_node')),
                             ('operations', ('view_operation', 'cancel_operation', 'recover_operation'))):
            for action in actions:
                self.user.user_permissions.add(Permission.objects.get(content_type__app_label=app, codename=action))
        self.token = services.issue_token(self.user.pk)
        self.actor = services.token_context(self.token)
        executor_token = services.enroll_service('node-executor', self.node.pk,
            project_ids=(self.project.pk,), permissions=('catalog.record_node_application', 'catalog.observe_node'))
        self.executor = services.token_context(executor_token)
        self.operations = operations_service
        self.api = self.operations(executor=self.executor)

    def command(self, **changes):
        from mop.server.operations.node import NodeEligibility
        values = dict(epoch=self.epoch, request_id=uuid4(), node_id=self.node.pk,
            expected_revision=1, not_after=timezone.now() + timedelta(minutes=2), eligible=False)
        values.update(changes)
        return NodeEligibility(**values)

    def submit(self, command=None):
        return self.api.submit(self.actor, command or self.command())

    # BUG: предметный Operations сам импортирует controller и получает доверие.
    # HYPOTHESIS: явная root composition должна быть обязательной для admission.
    # RED: PG16 — Refused not raised, unbound service принимает команду.
    # RED: PG17 — unbound publisher передаёт event до отказа на ACK transaction.
    # SOLUTION: root внедряет locked frozen state и прежний short_transaction;
    #           предметный сервис без обоих bindings отказывает.
    # RESULT: guard generation проверяется и без смены epoch до domain locks.
    # STATUS: FIXED — see #625
    def test_unbound_service_denies_even_with_ready_installation(self):
        with self.assertRaisesRegex(self.s.Refused, 'controller_unavailable'):
            self.s.Operations().submit(self.actor, self.command())
        self.assertEqual(self.m.Operation.objects.count(), 0)
        self.assertEqual(self.m.OutboxEvent.objects.count(), 0)

    def test_unbound_service_cannot_publish_or_discover_work(self):
        self.submit()
        seen = []
        with self.assertRaisesRegex(self.s.Refused, 'controller_unavailable'):
            self.s.Operations().publish_one(seen.append)
        self.assertEqual(seen, [])
        with self.assertRaisesRegex(self.s.Refused, 'controller_unavailable'):
            self.s.Operations().pending()
        self.assertIsNone(self.m.OutboxEvent.objects.get().published_at)

    # HYPOTHESIS: build discovery теряет фильтр node-only worker и выбирает restart.
    # SOLUTION: node-only accepted lane плюс отдельная stale-build inspection ветвь.
    # RESULT: PG16 — первым выбран UUID restart; после fix PG17 narrow 4/4 green.
    # STATUS: FIXED — see #623.
    # Машинные Step остаются в общей таблице, но потребляются только machine port.
    def test_controller_worker_does_not_consume_machine_restart_lane(self):
        from mop.server.controller.models import RestoreGuard
        self.m.Operation.objects.create(actor=self.user, actor_binding={}, request_id=uuid4(),
            digest='a'*64, epoch=self.epoch, restore_generation=RestoreGuard.objects.get().generation,
            action='restart', node=self.node, expected_revision=1, eligible=None,
            target_kind='puppet', target_id=uuid4(), incarnation_id=uuid4(),
            not_after=timezone.now()+timedelta(minutes=2))
        queued = self.submit()
        self.assertEqual(self.api.pending(limit=1), (UUID(queued['id']),))
        build = self.m.Operation.objects.create(actor=self.user, actor_binding={}, request_id=uuid4(),
            digest='b'*64, epoch=self.epoch, restore_generation=RestoreGuard.objects.get().generation,
            action='build', node=self.node, expected_revision=1, eligible=False, state='unknown',
            not_after=timezone.now()+timedelta(minutes=2))
        self.m.Step.objects.create(operation=build, name='image_build', lease_until=timezone.now()-timedelta(seconds=1))
        self.assertEqual(set(self.api.pending()), {UUID(queued['id']), build.id})

    def test_restore_generation_change_without_epoch_change_fences_claim_and_result(self):
        from mop.server.controller.models import RestoreGuard
        operation = self.submit()
        claim = self.api.claim(operation['id'])
        queued = self.submit(self.command(expected_revision=2))
        RestoreGuard.objects.update(generation=uuid4())
        self.assertIsNone(self.api.claim(queued['id']))
        with self.assertRaisesRegex(self.s.Refused, 'claim_conflict'):
            self.api.prepare_effect(claim)
        with self.assertRaisesRegex(self.s.Refused, 'claim_conflict'):
            self.api.complete(claim, expected_revision=claim.revision, applied=True)
        self.assertEqual(self.m.EffectReceipt.objects.count(), 0)
        self.assertTrue(self.m.ResourceClaim.objects.filter(operation_id=operation['id'], released_at=None).exists())

    def test_root_reader_is_locked_frozen_plain_state_and_refreshes_effect_gate(self):
        from dataclasses import FrozenInstanceError
        from mop.server.controller.models import Installation, RestoreGuard
        from mop.server.controller.runtime import operation_installation_state, short_transaction, materialized
        with self.assertRaisesRegex(RuntimeError, 'requires an application transaction'):
            operation_installation_state()
        with short_transaction():
            state = operation_installation_state()
            self.assertEqual(state.epoch, self.epoch)
            self.assertEqual(state.generation, RestoreGuard.objects.get(pk=1).generation)
            self.assertEqual(materialized(state), state)
            with self.assertRaises(FrozenInstanceError):
                state.effects_enabled = False
        operation = self.submit()
        claim = self.api.claim(operation['id'])
        Installation.objects.update(effects_enabled=False)
        self.assertIsNone(self.api.prepare_effect(claim))
        row = self.m.Operation.objects.get(pk=operation['id'])
        self.assertEqual((row.state, row.code), ('refused', 'effects_disabled'))
        self.assertFalse(self.m.ResourceClaim.objects.filter(operation=row, released_at=None).exists())

    def test_manual_ambient_transaction_cannot_return_uncommitted_acceptance(self):
        connection = connections['default']
        connection.set_autocommit(False)
        try:
            with self.assertRaisesRegex(self.s.Refused, 'transaction_boundary'):
                self.submit()
        finally:
            connection.rollback()
            connection.set_autocommit(True)
        self.assertEqual(self.m.Operation.objects.count(), 0)

    def test_acceptance_commits_request_step_audit_and_outbox_without_effect(self):
        operation = self.submit()
        self.assertEqual(operation['state'], 'accepted')
        self.assertEqual(self.m.Operation.objects.count(), 1)
        self.assertEqual(self.m.Step.objects.count(), 1)
        self.assertEqual(self.m.AuditEvent.objects.count(), 1)
        self.assertEqual(self.m.OutboxEvent.objects.count(), 1)
        self.assertEqual(self.m.EffectReceipt.objects.count(), 0)
        with ThreadPoolExecutor(max_workers=1) as pool:
            def read():
                try:
                    return self.m.Operation.objects.get(pk=operation['id']).state
                finally:
                    connections.close_all()
            self.assertEqual(pool.submit(read).result(), 'accepted')

    def test_fresh_process_observes_committed_result_without_local_queue(self):
        import multiprocessing
        operation = self.submit()
        context = multiprocessing.get_context('spawn')
        result = context.Queue()
        process = context.Process(target=_read_in_process, args=(connections['default'].settings_dict['NAME'],
            os.environ['MOP_CONTROLLER_BOOTSTRAP'], self.actor, operation['id'], result))
        process.start()
        try:
            self.assertEqual(result.get(timeout=20)['id'], operation['id'])
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            result.close()

    def test_independent_processes_have_one_durable_claim_winner(self):
        import multiprocessing
        operation = self.submit()
        context = multiprocessing.get_context('spawn')
        result, ready = context.Queue(), context.Event()
        children = [context.Process(target=_claim_in_process, args=(connections['default'].settings_dict['NAME'],
            os.environ['MOP_CONTROLLER_BOOTSTRAP'], operation['id'], ready, result)) for _ in range(2)]
        for child in children:
            child.start()
        ready.set()
        try:
            claims = [result.get(timeout=25) for _ in children]
            self.assertEqual(sum(claim is not None for claim in claims), 1)
            for child in children:
                child.join(timeout=10)
                self.assertEqual(child.exitcode, 0)
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                    child.join(timeout=5)
            result.close()

    def test_multi_resource_claims_block_another_node_in_same_project(self):
        from mop.server.catalog.models import Node, NodeProject
        other = Node.objects.create(name='other', driver='host', ssh_host='example.invalid',
            control_origin='https://example.invalid', lan_address='example.invalid', bootstrap_address='example.invalid')
        NodeProject.objects.create(node=other, project=self.project)
        first = self.submit()
        second = self.submit(self.command(node_id=other.pk))
        claim = self.api.claim(first['id'])
        self.assertIsNone(self.api.claim(second['id']))
        self.assertEqual(self.m.ResourceClaim.objects.filter(operation_id=first['id'], released_at=None).count(), 2)
        self.api.complete(claim, expected_revision=claim.revision, applied=True)
        self.assertIsNotNone(self.api.claim(second['id']))

    def test_real_admission_lock_timeout_never_commits_an_acceptance(self):
        import time
        from threading import Event
        from django.db import OperationalError
        from mop.server.controller.models import Installation
        entered, release = Event(), Event()
        def hold():
            try:
                with transaction.atomic():
                    Installation.objects.select_for_update().get(pk=1)
                    entered.set()
                    release.wait(timeout=8)
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            holder = pool.submit(hold)
            self.assertTrue(entered.wait(timeout=5))
            before = time.monotonic()
            try:
                with self.assertRaises(OperationalError):
                    self.submit()
                self.assertLess(time.monotonic() - before, 5)
            finally:
                release.set()
                holder.result(timeout=5)
        self.assertEqual(self.m.Operation.objects.count(), 0)
        self.assertEqual(self.m.OutboxEvent.objects.count(), 0)

    def test_shared_project_admission_bound_is_not_a_process_local_counter(self):
        api = self.operations(project_limit=1)
        api.submit(self.actor, self.command())
        with self.assertRaisesRegex(self.s.Refused, 'admission_limit'):
            self.operations(project_limit=1).submit(self.actor, self.command())
        self.assertEqual(self.m.Operation.objects.count(), 1)

    def test_shared_admission_bound_and_atomic_rollback(self):
        api = self.operations(queue_limit=1)
        api.submit(self.actor, self.command())
        with self.assertRaisesRegex(self.s.Refused, 'admission_limit'):
            api.submit(self.actor, self.command())
        self.assertEqual(self.m.Operation.objects.count(), 1)
        from unittest.mock import patch
        # Durable outbox failure rolls back request and audit; no accepted reply.
        self.m.Operation.objects.all().update(state='cancelled')
        with patch.object(self.m.OutboxEvent.objects, 'create', side_effect=RuntimeError('disk unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'disk unavailable'):
                api.submit(self.actor, self.command())
        self.assertEqual(self.m.Operation.objects.count(), 1)

    def test_identical_retry_precedes_deadline_and_revision_but_not_epoch_or_permission(self):
        command = self.command()
        operation = self.submit(command)
        from mop.server.catalog.models import Node
        Node.objects.filter(pk=self.node.pk).update(revision=2)
        self.assertEqual(self.submit(command)['id'], operation['id'])
        from unittest.mock import patch
        with patch('mop.server.operations.service.timezone.now', return_value=command.not_after + timedelta(seconds=1)):
            self.assertEqual(self.submit(command)['id'], operation['id'])
        conflict = self.command(request_id=command.request_id, eligible=True)
        with self.assertRaisesRegex(self.s.Refused, 'request_conflict'):
            self.submit(conflict)
        with self.assertRaisesRegex(self.s.Refused, 'epoch_conflict'):
            self.submit(self.command(epoch=uuid4(), request_id=command.request_id))
        self.user.user_permissions.clear()
        from mop.server.access.services import Denied
        with self.assertRaises(Denied):
            self.submit(command)

    def test_duplicate_independent_connections_admit_once(self):
        command = self.command()
        barrier = Barrier(2)
        def admit():
            try:
                barrier.wait(timeout=5)
                return self.submit(command)['id']
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: admit(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.m.Operation.objects.count(), 1)
        self.assertEqual(self.m.OutboxEvent.objects.count(), 1)

    def test_receipt_cas_fence_and_result_are_atomic(self):
        operation = self.submit()
        claim = self.api.claim(operation['id'])
        self.assertIsNotNone(claim)
        self.assertEqual(self.api.status(self.actor, operation['id'])['state'], 'running')
        self.assertIsNone(self.api.claim(operation['id']))
        with self.assertRaisesRegex(self.s.Refused, 'revision_conflict'):
            self.api.complete(claim, expected_revision=999, applied=True)
        self.assertEqual(self.m.EffectReceipt.objects.count(), 0)
        result = self.api.complete(claim, expected_revision=claim.revision, applied=True)
        self.assertEqual(result['state'], 'succeeded')
        self.assertFalse(result['resources_held'])
        from mop.server.catalog.models import Node
        node = Node.objects.get(pk=self.node.pk)
        self.assertFalse(node.desired_eligible)
        self.assertFalse(node.observed_eligible)
        self.assertEqual(node.revision, 2)
        self.assertEqual(node.applied_revision, 2)
        self.assertEqual(self.m.OutboxEvent.objects.order_by('-id').first().target_revision, node.revision)
        self.assertEqual(self.m.AuditEvent.objects.order_by('-id').first().target_revision, node.revision)
        self.assertEqual(result['target']['revision'], str(node.revision))
        self.assertEqual(self.m.EffectReceipt.objects.count(), 1)
        self.assertEqual(self.api.complete(claim, expected_revision=claim.revision, applied=True)['id'], result['id'])

    def test_unknown_holds_all_resources_until_bound_quiescent_receipt(self):
        first = self.submit()
        claim = self.api.claim(first['id'])
        unknown = self.api.uncertain(claim, expected_revision=claim.revision)
        self.assertEqual(unknown['state'], 'unknown')
        self.assertTrue(unknown['resources_held'])
        # Intent revision уже 2, но первый физический эффект ещё возможен.
        second = self.submit(self.command(expected_revision=2))
        self.assertIsNone(self.api.claim(second['id']))
        self.assertEqual(self.api.status(self.actor, second['id'])['state'], 'accepted')
        self.api.complete(claim, expected_revision=unknown['revision'], applied=True)
        self.assertIsNotNone(self.api.claim(second['id']))

    def test_auth_downgrade_before_claim_prevents_effect(self):
        operation = self.submit()
        self.user.user_permissions.clear()
        self.assertIsNone(self.api.claim(operation['id']))
        row = self.m.Operation.objects.get(pk=operation['id'])
        self.assertEqual(row.state, 'refused')
        self.assertEqual(row.reason, 'action permission required')
        self.assertEqual(self.m.ResourceClaim.objects.filter(operation_id=operation['id'], released_at=None).count(), 0)

    def test_expiry_tombstone_is_never_new_admission(self):
        command = self.command()
        operation = self.submit(command)
        self.api.cancel(self.actor, operation['id'], expected_revision=1)
        self.m.Operation.objects.filter(pk=operation['id']).update(completed_at=timezone.now() - timedelta(days=91))
        self.api.prune()
        with self.assertRaisesRegex(self.s.Refused, 'result_expired'):
            self.submit(command)
        self.assertEqual(self.m.Operation.objects.count(), 1)

    def test_retry_does_not_extend_deadline_and_unknown_never_reexecutes(self):
        operation = self.submit()
        self.m.Operation.objects.filter(pk=operation['id']).update(not_after=timezone.now() - timedelta(seconds=1))
        self.assertIsNone(self.api.claim(operation['id']))
        self.assertEqual(self.m.Operation.objects.get(pk=operation['id']).state, 'expired')
        next_operation = self.submit()
        claim = self.api.claim(next_operation['id'])
        unknown = self.api.uncertain(claim, expected_revision=claim.revision)
        self.assertIsNone(self.api.claim(next_operation['id']))
        self.assertTrue(unknown['resources_held'])
        # Перезапуск владельца не получает новую generation без quiescence.
        self.assertIsNone(self.operations().claim(next_operation['id']))

    def test_stale_fence_and_restored_epoch_cannot_commit_receipt(self):
        from dataclasses import replace
        from mop.server.controller.models import Installation
        operation = self.submit()
        claim = self.api.claim(operation['id'])
        with self.assertRaisesRegex(self.s.Refused, 'claim_conflict'):
            self.api.complete(replace(claim, token=uuid4()), expected_revision=claim.revision, applied=True)
        Installation.objects.update(epoch=uuid4(), effects_enabled=False)
        with self.assertRaisesRegex(self.s.Refused, 'epoch_conflict'):
            self.api.complete(claim, expected_revision=claim.revision, applied=True)
        self.assertTrue(self.m.ResourceClaim.objects.filter(operation_id=operation['id'], released_at=None).exists())

    def test_http_accepted_status_and_request_lookup_are_real_no_store(self):
        command = self.command()
        body = dict(schema_version=1, epoch=str(command.epoch), request_id=str(command.request_id),
            expected_revision='1', not_after=command.not_after.isoformat().replace('+00:00', 'Z'), params={})
        client = Client(HTTP_AUTHORIZATION='Token ' + self.token)
        url = f'/api/v1/nodes/{self.node.pk}/commands/drain'
        response = client.post(url, body, content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 202, response.content)
        self.assertIn('no-store', response.headers['Cache-Control'])
        op = response.json()['operation']
        self.assertTrue(self.m.Operation.objects.filter(pk=op['id']).exists())
        self.assertEqual(client.get(response.headers['Location'], secure=True).json()['operation']['id'], op['id'])
        lookup = client.get('/api/v1/operations', {'request_id': str(command.request_id), 'epoch': str(self.epoch)}, secure=True)
        self.assertEqual(lookup.json()['operation']['id'], op['id'])
        bad = client.post(url, {**body, 'argv': ['unsafe']}, content_type='application/json', secure=True)
        self.assertEqual(bad.status_code, 400)

    def test_outbox_lost_ack_repeats_same_event_id_not_a_new_event(self):
        self.submit()
        seen = []
        def lost_ack(event):
            seen.append(event)
            raise TimeoutError('publication acknowledgment lost')
        with self.assertRaises(TimeoutError):
            self.api.publish_one(lost_ack)
        self.api.publish_one(seen.append)
        self.assertEqual(seen[0]['event_id'], seen[1]['event_id'])
        self.assertEqual(self.m.OutboxEvent.objects.count(), 1)

    def test_result_outbox_failure_rolls_back_receipt_and_keeps_fence(self):
        from unittest.mock import patch
        operation = self.submit()
        claim = self.api.claim(operation['id'])
        with patch.object(self.m.OutboxEvent.objects, 'create', side_effect=RuntimeError('disk unavailable')):
            with self.assertRaises(RuntimeError):
                self.api.complete(claim, expected_revision=claim.revision, applied=True)
        self.assertEqual(self.m.Operation.objects.get(pk=operation['id']).state, 'running')
        self.assertEqual(self.m.EffectReceipt.objects.count(), 0)
        self.assertEqual(self.m.ResourceClaim.objects.filter(operation_id=operation['id'], released_at=None).count(), 2)
        self.assertTrue(self.api.uncertain(claim, expected_revision=claim.revision)['resources_held'])

    def test_current_executor_auth_is_required_but_completed_fact_survives_owner_revoke(self):
        from mop.server.access.services import Denied
        operation = self.submit()
        claim = self.api.claim(operation['id'])
        with self.assertRaises(Denied):
            self.operations().complete(claim, expected_revision=claim.revision, applied=True)
        self.user.user_permissions.clear()
        self.assertEqual(self.api.complete(claim, expected_revision=claim.revision, applied=True)['state'], 'succeeded')

    def test_manual_reconciliation_reauthorizes_and_never_reexecutes(self):
        from mop.server.access.services import Denied
        operation = self.submit()
        claim = self.api.claim(operation['id'])
        result = self.api.reconcile(self.actor, operation['id'], expected_revision=claim.revision)
        self.assertEqual(result['state'], 'reconciling')
        self.assertTrue(result['resources_held'])
        self.assertIsNone(self.api.claim(operation['id']))
        self.user.user_permissions.clear()
        with self.assertRaises(Denied):
            self.api.reconcile(self.actor, operation['id'], expected_revision=result['revision'])

    def test_fresh_worker_inspection_recovers_receipt_without_new_generation_or_effect(self):
        operation = self.submit()
        original = self.api.claim(operation['id'])
        unknown = self.api.uncertain(original, expected_revision=original.revision)
        restarted = self.operations(executor=self.executor)
        inspection = restarted.inspection_claim(self.actor, operation['id'], expected_revision=unknown['revision'])
        self.assertEqual(inspection.token, original.token)
        self.assertEqual(inspection.step_id, original.step_id)
        self.assertIsNone(restarted.claim(operation['id']))
        result = restarted.complete(inspection, expected_revision=inspection.revision, applied=True)
        self.assertEqual(result['state'], 'succeeded')
        self.assertFalse(result['resources_held'])
        self.assertEqual(self.m.EffectReceipt.objects.count(), 1)

    def test_restore_fence_is_a_safe_503_not_an_acceptance_or_server_crash(self):
        from mop.server.controller.models import RestoreGuard
        RestoreGuard.objects.update(state='restoring')
        command = self.command()
        body = dict(schema_version=1, epoch=str(command.epoch), request_id=str(command.request_id),
            expected_revision='1', not_after=command.not_after.isoformat().replace('+00:00', 'Z'), params={})
        response = Client(HTTP_AUTHORIZATION='Token ' + self.token).post(
            f'/api/v1/nodes/{self.node.pk}/commands/drain', body, content_type='application/json', secure=True)
        self.assertEqual(response.status_code, 503)
        self.assertIn('restore guard', response.json()['error']['message'])
        self.assertEqual(self.m.Operation.objects.count(), 0)

    def test_repeated_receipt_still_rejects_a_different_expected_revision(self):
        operation = self.submit()
        claim = self.api.claim(operation['id'])
        self.api.complete(claim, expected_revision=claim.revision, applied=True)
        with self.assertRaisesRegex(self.s.Refused, 'revision_conflict'):
            self.api.complete(claim, expected_revision=999, applied=True)

    def test_caller_cancel_and_shutdown_drain_the_owned_result_not_just_the_effect(self):
        operation = self.submit()
        api = self.api
        async def run():
            from mop.server.controller.runtime import Runtime
            from mop.server.operations.worker import Worker
            started, finish = asyncio.Event(), asyncio.Event()
            class Adapter:
                async def start(self): pass
                async def aclose(self): pass
                async def set_eligibility(self, claim):
                    started.set()
                    await finish.wait()
                    return True
            adapter = Adapter()
            runtime = Runtime(adapters=(adapter,), effect_limit=1)
            await runtime.start()
            worker = Worker(runtime, api, adapter)
            task = asyncio.create_task(worker.run_one(operation['id']))
            await asyncio.wait_for(started.wait(), timeout=10)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            held = await runtime.database(api.status, self.actor, operation['id'])
            self.assertEqual(held['state'], 'running')
            self.assertTrue(held['resources_held'])
            second = await runtime.database(api.submit, self.actor, self.command(expected_revision=2))
            queued = asyncio.create_task(worker.run_one(second['id']))
            await asyncio.sleep(0)
            shutdown = asyncio.create_task(runtime.aclose(timeout=10))
            await asyncio.sleep(0)
            finish.set()
            await shutdown
            await queued
            return second['id']
        second_id = asyncio.run(run())
        self.assertEqual(self.api.status(self.actor, second_id)['state'], 'accepted')
        self.assertEqual(self.api.status(self.actor, operation['id'])['state'], 'succeeded')

    def test_schema_has_no_uncommitted_model_delta(self):
        from django.core.management import call_command
        call_command('makemigrations', 'operations', check=True, dry_run=True, verbosity=0)

    def test_new_catalog_revision_cannot_trap_a_confirmed_quiescent_effect(self):
        from mop.server.catalog.models import Node
        from mop.server.catalog.services import Catalog
        from mop.server.access.services import Access
        operation = self.submit()
        claim = self.api.claim(operation['id'])
        # Access теперь требует CRUD + CONTROL право; fixture меняет catalog
        # настоящим service, не обходит актуальную authority (PG16 red: Denied).
        self.user.user_permissions.add(*Permission.objects.filter(content_type__app_label='catalog',
            codename__in=('change_node', 'configure_node')))
        Catalog(authorizer=Access()).set_deployment_membership(self.actor, self.node.pk, True, expected_revision=2)
        Node.objects.filter(pk=self.node.pk).update(observed_eligible=True)
        result = self.api.complete(claim, expected_revision=claim.revision, applied=True)
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['code'], 'effect_confirmed_prior_revision')
        self.assertFalse(result['resources_held'])
        self.assertFalse(result['evidence']['projection_updated'])
        node = Node.objects.get(pk=self.node.pk)
        self.assertEqual(node.revision, 3)
        self.assertIsNone(node.applied_revision)
        self.assertTrue(node.observed_eligible)
        self.assertEqual(self.m.EffectReceipt.objects.count(), 1)

    def test_claim_carries_the_unextendable_execution_deadline(self):
        from dataclasses import replace
        command = self.command()
        operation = self.submit(command)
        claim = self.api.claim(operation['id'])
        self.assertEqual(claim.not_after, command.not_after)
        with self.assertRaisesRegex(self.s.Refused, 'claim_conflict'):
            self.api.complete(replace(claim, not_after=claim.not_after + timedelta(days=1)),
                expected_revision=claim.revision, applied=True)

    def test_expiry_after_claim_commit_prevents_adapter_dispatch(self):
        from unittest.mock import patch
        from mop.server.controller.runtime import Runtime
        from mop.server.operations.worker import Worker
        now = timezone.now()
        command = self.command(not_after=now + timedelta(seconds=30))
        operation = self.submit(command)
        clock, effects = [now], []
        claim = self.api.claim
        def claim_then_expire(operation_id):
            value = claim(operation_id)
            clock[0] = command.not_after + timedelta(seconds=1)
            return value
        class Adapter:
            async def set_eligibility(self, claim):
                effects.append(claim.operation_id)
                return True
        async def run():
            runtime = Runtime()
            try:
                await Worker(runtime, self.api, Adapter()).run_one(operation['id'])
            finally:
                await runtime.aclose()
        with patch('mop.server.operations.service.timezone.now', side_effect=lambda: clock[0]), \
                patch.object(self.api, 'claim', side_effect=claim_then_expire):
            asyncio.run(run())
        self.assertEqual(effects, [])
        result = self.api.status(self.actor, operation['id'])
        self.assertEqual(result['state'], 'refused')
        self.assertEqual(result['code'], 'deadline_expired')
        self.assertFalse(result['resources_held'])

    def test_revocation_after_claim_commit_prevents_adapter_dispatch(self):
        from unittest.mock import patch
        from mop.server.controller.runtime import Runtime
        from mop.server.operations.worker import Worker
        operation = self.submit()
        effects, user_id = [], self.user.pk
        claim = self.api.claim
        def claim_then_revoke(operation_id):
            value = claim(operation_id)
            get_user_model().objects.get(pk=user_id).user_permissions.clear()
            return value
        class Adapter:
            async def set_eligibility(self, claim):
                effects.append(claim.operation_id)
                return True
        async def run():
            runtime = Runtime()
            try:
                await Worker(runtime, self.api, Adapter()).run_one(operation['id'])
            finally:
                await runtime.aclose()
        with patch.object(self.api, 'claim', side_effect=claim_then_revoke):
            asyncio.run(run())
        self.assertEqual(effects, [])
        row = self.m.Operation.objects.get(pk=operation['id'])
        self.assertEqual((row.state, row.code), ('refused', 'access_denied'))
        self.assertFalse(self.m.ResourceClaim.objects.filter(operation=row, released_at=None).exists())

    def test_worker_effect_and_outbox_publish_are_outside_atomic(self):
        operation = self.submit()
        api = self.api
        class Adapter:
            async def start(self): pass
            async def aclose(self): pass
            async def set_eligibility(inner, claim):
                self.assertFalse(transaction.get_connection().in_atomic_block)
                return True
        async def run():
            from mop.server.controller.runtime import Runtime
            from mop.server.operations.worker import Worker
            adapter = Adapter()
            runtime = Runtime(adapters=(adapter,))
            await runtime.start()
            try:
                worker = Worker(runtime, api, adapter)
                await worker.run_one(operation['id'])
            finally:
                await runtime.aclose()
        asyncio.run(run())
        self.assertEqual(self.api.status(self.actor, operation['id'])['state'], 'succeeded')
        emitted = []
        def publish(event):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            emitted.append(event)
        self.api.publish_one(publish)
        self.assertEqual(len(emitted), 1)
        self.assertNotIn('token', str(emitted))
