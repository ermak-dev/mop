"""#656: durable intent до destructive read, bounded ingestion, честные queries.

HYPOTHESIS: codec без durable intent теряет и повторно считает события после crash.
SPEC: read intent COMMIT до GET; timeout не quiescence; source всегда неполный.
RED: PG16 — 13 assertions отсутствующей durable source schema, baseline 163 green.
SOLUTION: source-row coordination, commit-before-pop intents, immutable identities,
nullable category coverage, strict global DRF query, bounded Runtime/typed boundary.
RESULT: PG16 15 checks green, PG17 16 (с owned HTTPS fixture) green; source profile
и remote exclusion на production не проверялись. CI — отдельный exact-SHA gate.
STATUS: FIXED — see #656 (durable core; production binding/cutover отдельно).
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import importlib
import json
from threading import Barrier
from uuid import uuid4

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connections, transaction
from django.test import TransactionTestCase, Client, override_settings
from django.utils import timezone
from proxy_usage import record, SALT


def _process_reader(capability, owner, barrier, result):
    from mop.server.sessions.usage_services import UsageStore, Held
    connections.close_all()
    try:
        barrier.wait(timeout=15)
        try:
            permit = UsageStore().begin(capability, owner_id=owner, count=10)
            answer = str(permit.intent_id)
        except Held:
            answer = 'held'
        from django.db import connection
        result.put((answer, connection.connection.info.backend_pid))
    finally:
        connections.close_all()


class ProxyUsage(TransactionTestCase):
    def setUp(self):
        self.assertIsNotNone(apps.get_app_config('execution').models.get('usagesource'),
            'proxy source/read intent/events/aggregates must be durable ORM state')
        self.s = importlib.import_module('mop.server.sessions.usage_services')
        self.p = importlib.import_module('mop.server.sessions.usage_protocol')
        self.m = importlib.import_module('mop.server.sessions.usage_models')
        self.store = self.s.UsageStore()
        self.remote = uuid4()
        self.boundary = uuid4()
        self.source = self.store.create_source(remote_id=self.remote, boundary_id=self.boundary,
            profile=self.p.PROFILE, installed_version='v8.0.16')
        self.owner = uuid4()

    def run_async(self, function):
        # Runtime закрывает process pool после quiescence. Setup connection
        # принадлежит sync test thread: вернуть её ДО owned runtime lifecycle,
        # а не оставлять borrowed socket в закрываемом pool до flush.
        connections.close_all()
        try:
            return asyncio.run(function())
        finally:
            connections.close_all()

    def capability(self, **changes):
        values = dict(source_id=self.source, remote_id=self.remote, boundary_id=self.boundary,
            profile=self.p.PROFILE, installed_version='v8.0.16',
            valid_until=timezone.now()+timedelta(seconds=30))
        values.update(changes)
        return self.p.ReadCapability(**values)

    def begin(self):
        return self.store.begin(self.capability(), owner_id=self.owner, count=10)

    def batch(self, records=None):
        from mop.server.sessions.proxy_usage import decode_batch
        return decode_batch(json.dumps([record()] if records is None else records).encode(),
            source_id=self.source, key_salt=SALT)

    def finish(self, intent, records=None):
        return self.store.finish(intent, self.batch(records))

    def test_intent_is_committed_before_read_and_expiry_never_allows_another_pop(self):
        intent = self.begin()
        self.assertTrue(self.m.UsageReadIntent.objects.filter(pk=intent.intent_id, state='reading').exists())
        self.m.UsageReadIntent.objects.filter(pk=intent.intent_id).update(started_at=timezone.now()-timedelta(days=1))
        with self.assertRaises(self.s.Held):
            self.begin()
        self.assertEqual(self.m.UsageReadIntent.objects.count(), 1)

    # HYPOTHESIS: atomic внутри manual transaction не коммитит read intent.
    # SOLUTION: admission требует autocommit=True и отсутствие внешнего atomic.
    # RESULT: PG16 red (Unavailable не raised), затем PG16/17 по 18/18 green.
    # STATUS: FIXED — see #656
    def test_manual_transaction_cannot_return_an_uncommitted_read_permit(self):
        connection = connections['default']
        transaction.set_autocommit(False)
        try:
            self.assertFalse(connection.in_atomic_block)
            with self.assertRaises(self.s.Unavailable):
                self.begin()
        finally:
            transaction.rollback()
            transaction.set_autocommit(True)
        self.assertFalse(self.m.UsageReadIntent.objects.filter(source_id=self.source).exists())

    def test_outer_transaction_cannot_return_a_read_permit(self):
        with transaction.atomic():
            with self.assertRaises(self.s.Unavailable):
                self.begin()
        self.assertEqual(self.m.UsageReadIntent.objects.count(), 0)

    def test_competing_independent_connections_have_one_reader(self):
        barrier = Barrier(2)
        def reader():
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                try:
                    return self.begin().intent_id
                except self.s.Held:
                    return None
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: reader(), range(2)))
        self.assertEqual(sum(v is not None for v in results), 1)

    def test_independent_processes_cannot_share_a_destructive_queue_read(self):
        import multiprocessing
        context = multiprocessing.get_context('fork')
        barrier, result = context.Barrier(2), context.Queue()
        capability = self.capability()
        # Pool sockets/driver threads нельзя наследовать fork: close_all лишь
        # возвращает connection в pool. Останавливаем pool до создания процессов.
        from mop.server.controller.runtime import close_pools
        close_pools()
        workers = [context.Process(target=_process_reader, args=(capability, uuid4(), barrier, result))
                   for _ in range(2)]
        for worker in workers:
            worker.start()
        try:
            answers = [result.get(timeout=25) for _ in workers]
            self.assertEqual(sum(answer == 'held' for answer, _ in answers), 1)
            self.assertEqual(len({pid for _, pid in answers}), 2,
                             'processes must use independent PostgreSQL connections')
        finally:
            for worker in workers:
                worker.join(timeout=15)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)
            result.close()
            result.join_thread()
        self.assertTrue(all(worker.exitcode == 0 for worker in workers))
        self.assertEqual(self.m.UsageReadIntent.objects.count(), 1)

    def test_pop_to_commit_crash_preserves_intent_and_transaction_rollback(self):
        from unittest.mock import patch
        intent = self.begin()
        with patch.object(self.m.UsageAggregate.objects, 'get_or_create', side_effect=RuntimeError('crash after pop')):
            with self.assertRaises(RuntimeError):
                self.finish(intent)
        self.assertEqual(self.m.UsageEvent.objects.count(), 0)
        self.assertEqual(self.m.UsageReadIntent.objects.get().state, 'reading')
        with self.assertRaises(self.s.Held):
            self.begin()
        self.finish(intent)
        self.assertEqual(self.m.UsageEvent.objects.count(), 1)

    def test_unknown_after_external_failure_holds_source_until_addressed_quiescence(self):
        intent = self.begin()
        self.store.unknown(intent, code='transport_unknown')
        with self.assertRaises(self.s.Held):
            self.begin()
        wrong = self.p.Quiescence(self.source, uuid4(), self.boundary, timezone.now())
        with self.assertRaises(self.s.Held):
            self.store.reconcile(intent, wrong)
        self.store.reconcile(intent, self.p.Quiescence(self.source, intent.intent_id, self.boundary, timezone.now()))
        self.assertNotEqual(self.begin().intent_id, intent.intent_id)
        old = self.m.UsageReadIntent.objects.get(pk=intent.intent_id)
        self.assertEqual(old.state, 'gap')
        self.assertEqual(old.gap_code, 'read_outcome_unknown')
        with self.assertRaises(self.s.Held):
            self.finish(intent)

    def test_restart_and_idempotent_commit_do_not_count_the_same_event_twice(self):
        first = self.begin()
        self.finish(first)
        self.store = self.s.UsageStore()
        self.finish(first)
        self.finish(self.begin())
        self.assertEqual(self.m.UsageEvent.objects.count(), 1)
        aggregate = self.m.UsageAggregate.objects.get()
        self.assertEqual(aggregate.requests, 1)
        self.assertEqual(aggregate.totals['total'], 41)
        self.assertEqual(aggregate.known['total'], 1)

    def test_payload_conflict_preserves_original_and_records_a_gap(self):
        self.finish(self.begin())
        changed = record()
        changed['alias'] = 'different'
        result = self.finish(self.begin(), [changed])
        self.assertEqual(result.conflicts, 1)
        self.assertEqual(self.m.UsageEvent.objects.get().alias, 'opus')
        self.assertEqual(self.m.UsageAggregate.objects.get().requests, 1)
        self.assertEqual(self.m.UsageReadIntent.objects.order_by('-started_at').first().gap_code, 'identity_conflict')

    def test_empty_and_rejected_batches_are_not_complete_or_measured_zero(self):
        self.finish(self.begin(), [])
        self.finish(self.begin(), ['invalid record'])
        self.assertEqual(self.m.UsageAggregate.objects.count(), 0)
        self.assertFalse(self.m.UsageSource.objects.get().source_complete)
        self.assertEqual(self.m.UsageReadIntent.objects.filter(gap_code='invalid_records').count(), 1)

    def test_partial_categories_and_utc_buckets_preserve_known_unknown_coverage(self):
        value = record()
        value['timestamp'] = '2026-10-17T00:03:04+02:00'
        del value['token_breakdown']['input']['cache_write_tokens']
        self.finish(self.begin(), [value])
        row = self.m.UsageAggregate.objects.get()
        self.assertEqual(row.day.isoformat(), '2026-10-16')
        self.assertNotIn('cache_write', row.totals)
        self.assertEqual(row.known.get('cache_write', 0), 0)
        self.assertEqual(row.totals['total'], 41)

    def test_profile_and_remote_boundary_must_match_not_just_a_local_db_lease(self):
        for change in ({'boundary_id': uuid4()}, {'remote_id': uuid4()},
                       {'installed_version': 'v8.0.17'}, {'profile': 'invented'},
                       {'valid_until': timezone.now()-timedelta(seconds=1)}):
            with self.assertRaises(self.s.Unavailable):
                self.store.begin(self.capability(**change), owner_id=self.owner, count=10)
        self.assertEqual(self.m.UsageReadIntent.objects.count(), 0)
        with self.assertRaises(self.s.Unavailable):
            self.store.create_source(remote_id=uuid4(), boundary_id=uuid4(),
                profile=self.p.PROFILE, installed_version='unknown')

    def test_no_plaintext_private_queue_fields_are_persisted(self):
        self.finish(self.begin())
        data = repr(tuple(self.m.UsageEvent.objects.values())) + repr(tuple(self.m.UsageAggregate.objects.values()))
        for secret in ('sk-synthetic-private-client-656', 'upstream-private-key',
                       'private response body', 'private-header', 'private-session'):
            self.assertNotIn(secret, data)

    def actor(self, *, global_scope):
        from mop.server.access import services, models
        user = get_user_model().objects.create_user('global' if global_scope else 'project')
        services.bind_local_identity(user.pk)
        user.user_permissions.add(Permission.objects.get(content_type__app_label='execution', codename='view_usage'))
        if global_scope:
            source = models.IdentitySource.objects.get(kind='local')
            models.AllProjectMembership.objects.create(user=user, source=source)
        token = services.issue_token(user.pk)
        return services.token_context(token), token

    @override_settings(ALLOWED_HOSTS=['testserver'])
    def test_public_queries_require_global_usage_permission_and_never_invent_attribution(self):
        self.finish(self.begin())
        local, _ = self.actor(global_scope=False)
        from mop.server.access.services import Denied
        with self.assertRaises(Denied):
            self.s.UsageQueries().read(local, start='2026-10-16', end='2026-10-17')
        actor, token = self.actor(global_scope=True)
        result = self.s.UsageQueries().read(actor, start='2026-10-16', end='2026-10-17')
        self.assertFalse(result['source_complete'])
        self.assertEqual(result['attribution'], {'user': 'unavailable', 'project': 'unavailable', 'puppet': 'unavailable'})
        self.assertEqual(result['items'][0]['counters']['total'], {'value': '41', 'known_events': 1, 'unknown_events': 0})
        client = Client(HTTP_AUTHORIZATION='Token '+token)
        response = client.get('/api/v1/usage?schema_version=1&start=2026-10-16&end=2026-10-17', secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), result)
        from mop.client.usage import stat
        summary = stat(lambda **params: self.s.UsageQueries().read(actor, **params),
                       start='2026-10-16', end='2026-10-17')
        self.assertEqual(summary['groups'][0]['counters']['total']['value'], '41')
        self.assertFalse(summary['source_complete'])
        bad = client.get('/api/v1/usage?schema_version=1&start=2026-10-16&start=2026-10-15&end=2026-10-17', secure=True)
        self.assertEqual(bad.status_code, 400)

    def test_boundary_validation_errors_and_misaddressed_capability_fail_before_intent(self):
        from mop.server.controller.runtime import Runtime
        outer = self
        class BrokenBoundary:
            async def acquire(self, source_id):
                raise ValueError('private-boundary-credential')
        class WrongBoundary:
            async def acquire(self, source_id):
                return outer.capability(source_id=uuid4())
        async def run():
            for boundary in (BrokenBoundary(), WrongBoundary()):
                runtime = Runtime()
                collector = self.s.UsageCollector(runtime=runtime, source_id=self.source, boundary=boundary)
                try:
                    with self.assertRaises(self.s.Unavailable) as caught:
                        await collector.once()
                    self.assertNotIn('private-boundary-credential', str(caught.exception))
                finally:
                    await runtime.aclose()
        self.run_async(run)
        self.assertEqual(self.m.UsageReadIntent.objects.count(), 0)

    def test_unbound_collector_denies_before_any_read_intent(self):
        from mop.server.controller.runtime import Runtime
        async def run():
            runtime = Runtime()
            collector = self.s.UsageCollector(runtime=runtime, source_id=self.source)
            try:
                with self.assertRaises(self.s.Unavailable):
                    await collector.once()
            finally:
                await runtime.aclose()
        self.run_async(run)
        self.assertEqual(self.m.UsageReadIntent.objects.count(), 0)

    def test_isolated_https_proxy_consumes_tagged_counter_fixture_without_blocking_loop(self):
        from pathlib import Path
        import ssl
        import subprocess
        import tempfile
        import aiohttp
        from mop.server.controller.runtime import Runtime
        from mop.server.sessions.proxy_usage import decode_batch
        outer = self
        with tempfile.TemporaryDirectory(prefix='usage-https-fixture-') as directory:
            cert, key = Path(directory)/'cert.pem', Path(directory)/'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=localhost',
                '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True, timeout=15)
            server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_tls.load_cert_chain(cert, key)
            client_tls = ssl.create_default_context(cafile=str(cert))
            async def run():
                runtime, observed, ticks = Runtime(), [], []
                async def handle(reader, writer):
                    try:
                        header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout=2)
                        self.assertIn(b'GET /v8/management/observability/usage/queue?count=10 HTTP/1.1', header)
                        self.assertIn(b'Authorization: Bearer isolated-fixture-only', header)
                        committed = await runtime.database(lambda: outer.m.UsageReadIntent.objects.filter(state='reading').exists())
                        await asyncio.sleep(.1)
                        observed.append((committed, len(ticks)))
                        second = record()
                        second.update(execution_id='another-execution', model='another-model', api_key='second-fixture-key')
                        body = json.dumps([record(), second]).encode()
                        writer.write(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: '
                                     +str(len(body)).encode()+b'\r\n\r\n'+body)
                        await writer.drain()
                    finally:
                        writer.close()
                        await writer.wait_closed()
                server = await asyncio.start_server(handle, '127.0.0.1', 0, ssl=server_tls, limit=8192)
                port = server.sockets[0].getsockname()[1]
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2), trust_env=False) as client:
                    class Boundary:
                        async def acquire(self, source_id):
                            # Только owned fake endpoint: actual production exclusion здесь не утверждается.
                            return outer.capability()
                        async def pop(self, permit):
                            async with client.get(f'https://127.0.0.1:{port}/v8/management/observability/usage/queue?count={permit.count}',
                                    headers={'Authorization': 'Bearer isolated-fixture-only'}, ssl=client_tls,
                                    allow_redirects=False) as response:
                                outer.assertEqual(response.status, 200)
                                body = await response.read()
                            return decode_batch(body, source_id=permit.source_id, key_salt=SALT)
                    async def heartbeat():
                        for _ in range(8):
                            await asyncio.sleep(.02)
                            ticks.append(True)
                    collector = self.s.UsageCollector(runtime=runtime, source_id=self.source, boundary=Boundary(), count=10)
                    try:
                        result, _ = await asyncio.gather(collector.once(), heartbeat())
                        self.assertEqual(result['accepted'], 2)
                        self.assertEqual(len(observed), 1)
                        self.assertTrue(observed[0][0])
                        self.assertGreaterEqual(observed[0][1], 2,
                            'heartbeat must advance while HTTPS response is pending')
                    finally:
                        server.close()
                        await server.wait_closed()
                        await runtime.aclose()
            self.run_async(run)
        self.assertEqual(self.m.UsageEvent.objects.count(), 2)
        self.assertEqual(self.m.UsageAggregate.objects.count(), 2)

    def test_async_collector_commits_before_io_and_persists_ambiguous_outcome(self):
        from mop.server.controller.runtime import Runtime
        outer = self
        class Boundary:
            async def acquire(self, source_id):
                return outer.capability()
            async def pop(self, permit):
                self.permit = permit
                def verify():
                    try:
                        outer.assertTrue(outer.m.UsageReadIntent.objects.filter(pk=permit.intent_id).exists())
                    finally:
                        connections.close_all()
                await asyncio.to_thread(verify)
                raise TimeoutError('private key must not appear in diagnostics')
        async def run():
            runtime = Runtime()
            collector = self.s.UsageCollector(runtime=runtime, source_id=self.source, boundary=Boundary())
            try:
                result = await collector.once()
                self.assertEqual(result['state'], 'unknown')
            finally:
                await runtime.aclose()
        self.run_async(run)
        intent = self.m.UsageReadIntent.objects.get()
        self.assertEqual(intent.state, 'unknown')
        self.assertEqual(intent.gap_code, 'transport_unknown')
        with self.assertRaises(self.s.Held):
            self.begin()
