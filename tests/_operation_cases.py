"""Поведенческий durable vertical #625 на независимых PostgreSQL connections."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from uuid import uuid4

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connections, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone


def _read_in_process(database, actor, operation_id, result):
    from mop.server.controller.runtime import configure
    configure()
    from django.conf import settings
    settings.DATABASES['default']['NAME'] = database
    from mop.server.operations.service import Operations
    try:
        result.put(Operations().status(actor, operation_id))
    finally:
        connections.close_all()


# FEATURE: durable admission, claim, receipt и REST result — один владелец.
# HYPOTHESIS: pure policies не переживают процесс и не защищают внешний эффект.
# SOLUTION: короткие ORM transactions, typed node handler и Runtime-owned worker.
# STATUS: RED — see #625; результат дополняется после настоящего PG запуска.
@override_settings(ALLOWED_HOSTS=['testserver'])
class DurableOperations(TransactionTestCase):
    def setUp(self):
        self.assertTrue(apps.is_installed('mop.server.operations'), 'durable Operations ORM must be installed')
        from mop.server.operations import models, service
        from mop.server.access import models as access, services
        from mop.server.catalog.models import Project, Node, NodeProject
        from mop.server.controller.runtime import initialize
        from mop.server.controller.models import Installation
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
        for action in ('drain_node', 'enable_node'):
            self.user.user_permissions.add(Permission.objects.get(content_type__app_label='catalog', codename=action))
        self.token = services.issue_token(self.user.pk)
        self.actor = services.token_context(self.token)
        self.api = service.Operations()

    def command(self, **changes):
        from mop.server.operations.node import NodeEligibility
        values = dict(epoch=self.epoch, request_id=uuid4(), node_id=self.node.pk,
            expected_revision=1, not_after=timezone.now() + timedelta(minutes=2), eligible=False)
        values.update(changes)
        return NodeEligibility(**values)

    def submit(self, command=None):
        return self.api.submit(self.actor, command or self.command())

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
            self.actor, operation['id'], result))
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

    def test_shared_admission_bound_and_atomic_rollback(self):
        api = self.s.Operations(queue_limit=1)
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
        self.assertEqual(self.m.EffectReceipt.objects.count(), 1)
        self.assertEqual(self.api.complete(claim, expected_revision=claim.revision, applied=True)['id'], result['id'])

    def test_unknown_holds_all_resources_until_bound_quiescent_receipt(self):
        first = self.submit()
        claim = self.api.claim(first['id'])
        unknown = self.api.uncertain(claim, expected_revision=claim.revision)
        self.assertEqual(unknown['state'], 'unknown')
        self.assertTrue(unknown['resources_held'])
        second = self.submit()
        self.assertIsNone(self.api.claim(second['id']))
        self.api.complete(claim, expected_revision=unknown['revision'], applied=True)
        # queued intent теперь stale, не запускается под новым revision.
        self.assertIsNone(self.api.claim(second['id']))
        self.assertEqual(self.api.status(self.actor, second['id'])['state'], 'refused')

    def test_auth_downgrade_before_claim_prevents_effect(self):
        operation = self.submit()
        self.user.user_permissions.clear()
        self.assertIsNone(self.api.claim(operation['id']))
        self.assertEqual(self.m.Operation.objects.get(pk=operation['id']).state, 'refused')
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
