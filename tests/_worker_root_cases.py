"""Поведенческий root worker gate без пула (#625)."""
import asyncio
from unittest import TestCase
from uuid import UUID

from django.test import TransactionTestCase

import _operation_cases as fixtures
import _build_cases as build_fixtures


# HYPOTHESIS: root только ждёт signal; durable очередь никто не читает,
# а отсутствие порта превращается в silent no-op или ошибку после claim.
# SOLUTION: конечная root composition, bounded poll и owned drain; machine
# restart остаётся за HTTP, privileged build за отдельными named ports.
# RED: PG16/17 — unconfigured startup и unbound build молча успешны;
#      poll/composition отсутствуют. PG16 — revoked executor получает dispatch,
#      blocking node port принят, ошибка owned эффекта скрыта при shutdown.
# HYPOTHESIS (REJECTED): cancel batch безопасен, раз Runtime shield держит эффект.
# SOLUTION: batch ждёт bounded drain без cancel; иначе теряется его ошибка.
# RESULT: реальные PG16/17 проверяют root poll/receipt, отдельный process,
#         current authority и bounded drain; настоящие PVE/node bindings не выдуманы.
# STATUS: FIXED — see #625
class WorkerLifecycle(TestCase):
    def test_default_worker_refuses_unconfigured_capabilities_and_closes_resources(self):
        from mop.server.controller.runtime import Runtime, run_worker
        class Resource:
            closed = False
            async def start(self):
                pass
            async def aclose(self):
                self.closed = True
        async def run():
            resource = Resource()
            stop = asyncio.Event()
            stop.set()
            with self.assertRaisesRegex(RuntimeError, 'worker capabilities are not configured'):
                await run_worker(stop, Runtime(adapters=(resource,)))
            self.assertTrue(resource.closed)
        asyncio.run(run())

    def test_poll_repeats_after_empty_pass_and_stops_without_another_batch(self):
        from mop.server.controller.runtime import Runtime, run_worker
        async def run():
            stop = asyncio.Event()
            class Consumer:
                calls = 0
                async def run_pending(self, *, limit):
                    self.calls += 1
                    self_limit.assertEqual(limit, 2)
                    if self.calls == 3:
                        stop.set()
                    return []
            self_limit = self
            consumer = Consumer()
            runtime = Runtime()
            await asyncio.wait_for(run_worker(stop, runtime, worker=consumer,
                batch_limit=2, poll_interval=.001), 2)
            self.assertEqual(consumer.calls, 3)
            self.assertFalse(runtime.accepting)
            self.assertEqual(runtime.pending, 0)
        asyncio.run(run())

    def test_poll_failure_is_not_silenced(self):
        from mop.server.controller.runtime import Runtime, run_worker
        class Consumer:
            async def run_pending(self, *, limit):
                raise RuntimeError('durable discovery failed')
        async def run():
            runtime = Runtime()
            with self.assertRaisesRegex(RuntimeError, 'durable discovery failed'):
                await run_worker(asyncio.Event(), runtime, worker=Consumer(), poll_interval=.001)
            self.assertFalse(runtime.accepting)
        asyncio.run(run())

    def test_cancelling_root_retains_owned_effect_until_result_and_adapter_cleanup(self):
        from mop.server.controller.runtime import Runtime, run_worker
        async def run():
            started, release = asyncio.Event(), asyncio.Event()
            events = []
            class Resource:
                async def start(self):
                    events.append('start')
                async def aclose(self):
                    events.append('close')
            runtime = Runtime(effect_limit=1, adapters=(Resource(),))
            class Consumer:
                async def run_pending(self, *, limit):
                    return await runtime.effect(effect)
            async def effect():
                started.set()
                await release.wait()
                events.append(await runtime.database(lambda: 'result'))
            task = asyncio.create_task(run_worker(asyncio.Event(), runtime,
                worker=Consumer(), poll_interval=.001))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(task.done())
            self.assertGreater(runtime.pending, 0)
            self.assertNotIn('close', events)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            self.assertEqual(events, ['start', 'result', 'close'])
            self.assertEqual(runtime.pending, 0)
        asyncio.run(run())

    def test_stop_during_discovery_does_not_start_a_new_claim(self):
        from threading import Event
        from mop.server.controller.runtime import Runtime, run_worker
        from mop.server.operations.worker import Worker
        async def run():
            entered, release = Event(), Event()
            stop = asyncio.Event()
            runtime = Runtime()
            class Operations:
                def pending(inner, **options):
                    entered.set()
                    if not release.wait(2):
                        raise TimeoutError('test discovery was not released')
                    return (UUID(int=1),)
                def handler(inner, operation_id):
                    self.fail('shutdown must not start another claim')
            class Node:
                async def set_eligibility(inner, claim):
                    self.fail('shutdown must not start another effect')
            consumer = Worker(runtime, Operations(), Node())
            task = asyncio.create_task(run_worker(stop, runtime, worker=consumer))
            try:
                async with asyncio.timeout(2):
                    while not entered.is_set():
                        await asyncio.sleep(.001)
                    stop.set()
                    while runtime.accepting:
                        await asyncio.sleep(.001)
                release.set()
                await asyncio.wait_for(task, 2)
                self.assertEqual(runtime.pending, 0)
            finally:
                release.set()
                await runtime.aclose()
        asyncio.run(run())

    def test_stopping_root_does_not_hide_failure_of_an_already_owned_pass(self):
        from mop.server.controller.runtime import Runtime, run_worker
        async def run():
            stop = asyncio.Event()
            runtime = Runtime()
            async def effect():
                stop.set()
                await asyncio.sleep(.02)
                raise RuntimeError('effect receipt failed')
            class Consumer:
                async def run_pending(self, *, limit):
                    return await runtime.effect(effect)
            with self.assertRaisesRegex(RuntimeError, 'effect receipt failed'):
                await run_worker(stop, runtime, worker=Consumer())
            self.assertEqual(runtime.pending, 0)
        asyncio.run(run())

    def test_shutdown_timeout_keeps_running_effect_and_adapter_owned(self):
        from mop.server.controller.runtime import Runtime, run_worker
        async def run():
            entered, release = asyncio.Event(), asyncio.Event()
            closed = []
            class Resource:
                async def start(self):
                    pass
                async def aclose(self):
                    closed.append(True)
            runtime = Runtime(effect_limit=1, adapters=(Resource(),))
            async def effect():
                entered.set()
                await release.wait()
                await runtime.database(lambda: None)
            class Consumer:
                async def run_pending(self, *, limit):
                    await runtime.effect(effect)
            stop = asyncio.Event()
            task = asyncio.create_task(run_worker(stop, runtime, worker=Consumer(), shutdown_timeout=.01))
            await asyncio.wait_for(entered.wait(), 2)
            stop.set()
            try:
                with self.assertRaisesRegex(RuntimeError, 'shutdown incomplete'):
                    await asyncio.wait_for(task, 2)
                self.assertGreater(runtime.pending, 0)
                self.assertEqual(closed, [])
            finally:
                release.set()
                await runtime.aclose()
            self.assertEqual(runtime.pending, 0)
            self.assertEqual(closed, [True])
        asyncio.run(run())

    def test_unbound_build_is_rejected_before_any_claim(self):
        from mop.server.controller.runtime import Runtime
        from mop.server.operations.worker import Worker
        class Operations:
            def handler(self, operation_id):
                return 'image_build'
            def claim(self, operation_id):
                raise AssertionError('unsupported work must not be claimed')
        async def run():
            runtime = Runtime()
            try:
                with self.assertRaisesRegex(RuntimeError, 'image_build.*not configured'):
                    await Worker(runtime, Operations(), None).run_one(UUID(int=1))
            finally:
                await runtime.aclose()
        asyncio.run(run())


class BuildWorkerRoot(TransactionTestCase):
    serialized_rollback = True
    setUp = fixtures.DurableOperations.setUp
    policy, body = build_fixtures.ImageBuilds.policy, build_fixtures.ImageBuilds.body
    permission, setup_build = build_fixtures.ImageBuilds.permission, build_fixtures.ImageBuilds.setup_build

    def test_root_binds_fenced_scheduler_and_build_handler_not_node_eligibility(self):
        from mop.server.controller.runtime import Runtime, compose_worker
        from mop.server.builds.adapters import FencedScheduler, FencedNodeExecutor
        self.setup_build(targets=1)
        world = self.fake
        class SchedulerClient:
            capabilities = frozenset({'conditional_spec', 'conditional_announce', 'inspect', 'complete_plan'})
            plan = staticmethod(world.plan)
            compare_and_apply_spec = staticmethod(world.apply)
            confirm_and_announce_image = staticmethod(world.apply)
            inspect = staticmethod(world.inspect)
        class NodeClient:
            capabilities = frozenset({'conditional_body', 'inspect'})
            wipe_stopped_incarnation = staticmethod(world.apply)
            inspect = staticmethod(world.inspect)
        async def run():
            runtime = Runtime(blocking_limit=1)
            worker = compose_worker(runtime, scheduler=FencedScheduler(client=SchedulerClient()),
                image_builder=world, build_node_executor=FencedNodeExecutor(client=NodeClient()))
            try:
                results = await worker.run_pending(limit=1)
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]['state'], 'succeeded')
            finally:
                await runtime.aclose()
        asyncio.run(run())
        self.assertEqual([stage for stage, _ in world.calls], ['clear', 'wipe', 'build', 'restore', 'announce'])
        self.assertFalse(self.m.ResourceClaim.objects.filter(released_at=None).exists())


class WorkerRoot(TransactionTestCase):
    serialized_rollback = True
    setUp = fixtures.DurableOperations.setUp
    command = fixtures.DurableOperations.command
    submit = fixtures.DurableOperations.submit

    def test_poll_discovers_committed_operation_after_empty_pass_and_drains_receipt(self):
        from mop.server.controller.runtime import Runtime, compose_worker, run_worker
        from django.db import connection
        async def run():
            stop, empty = asyncio.Event(), asyncio.Event()
            runtime = Runtime(effect_limit=1)
            calls = []
            class Adapter:
                async def set_eligibility(inner, claim):
                    self.assertFalse(connection.in_atomic_block)
                    calls.append(claim.operation_id)
                    stop.set()
                    await asyncio.sleep(.02)
                    return True
            worker = compose_worker(runtime, node_executor=Adapter(), executor=self.executor)
            # Сигнал пустого прохода ставит async wrapper, не ORM callback.
            original = worker.run_pending
            async def batch(**options):
                result = await original(**options)
                if not calls:
                    empty.set()
                return result
            worker.run_pending = batch
            task = asyncio.create_task(run_worker(stop, runtime, worker=worker, poll_interval=.005))
            await asyncio.wait_for(empty.wait(), 2)
            operation = await runtime.database(self.submit)
            await asyncio.wait_for(task, 3)
            self.assertEqual(calls, [UUID(operation['id'])])
            self.assertEqual(runtime.pending, 0)
            return operation
        operation = asyncio.run(run())
        row = self.m.Operation.objects.get(pk=operation['id'])
        self.assertEqual(row.state, 'succeeded')
        self.assertEqual(self.m.EffectReceipt.objects.count(), 1)
        self.assertFalse(self.m.ResourceClaim.objects.filter(released_at=None).exists())

    def test_fresh_worker_process_polls_an_operation_committed_by_another_process(self):
        import multiprocessing
        import os
        from django.db import connections
        from _operation_process import worker
        context = multiprocessing.get_context('spawn')
        ready, result = context.Event(), context.Queue()
        process = context.Process(target=worker, args=(connections.databases['default']['NAME'],
            os.environ['MOP_CONTROLLER_BOOTSTRAP'], self.executor, ready, result))
        process.start()
        try:
            self.assertTrue(ready.wait(10), 'owned worker did not start')
            operation = self.submit()
            process.join(15)
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(result.get(timeout=2), 0)
            self.assertEqual(self.m.Operation.objects.get(pk=operation['id']).state, 'succeeded')
            self.assertFalse(self.m.ResourceClaim.objects.filter(released_at=None).exists())
        finally:
            if process.is_alive():
                process.terminate()
                process.join(3)
            result.close()
            result.join_thread()

    def test_root_rejects_missing_or_partial_ports_before_claim(self):
        from mop.server.controller.runtime import Runtime, compose_worker
        async def run():
            runtime = Runtime()
            try:
                for options in ({}, {'node_executor': object()}, {'executor': self.executor},
                                {'scheduler': object()}, {'image_builder': object()},
                                {'build_node_executor': object()}):
                    with self.subTest(options=tuple(options)):
                        with self.assertRaisesRegex((RuntimeError, ValueError), 'configured|bound|required'):
                            compose_worker(runtime, **options)
            finally:
                await runtime.aclose()
        asyncio.run(run())
        self.assertEqual(self.m.ResourceClaim.objects.count(), 0)

    def test_root_rejects_blocking_node_port_before_claim(self):
        from mop.server.controller.runtime import Runtime, compose_worker
        class BlockingNode:
            def set_eligibility(inner, claim):
                self.fail('blocking node port must never reach the event loop')
        async def run():
            runtime = Runtime()
            try:
                with self.assertRaisesRegex(RuntimeError, 'managed async'):
                    compose_worker(runtime, node_executor=BlockingNode(), executor=self.executor)
            finally:
                await runtime.aclose()
        asyncio.run(run())
        self.assertEqual(self.m.ResourceClaim.objects.count(), 0)

    def test_prepare_effect_rechecks_executor_authority_after_claim_commit(self):
        from django.contrib.auth import get_user_model
        operation = self.submit()
        claim = self.api.claim(operation['id'])
        get_user_model().objects.filter(pk=self.executor.user_id).update(is_active=False)
        self.assertIsNone(self.api.prepare_effect(claim), 'revoked executor still receives a dispatch checkpoint')
        row = self.m.Operation.objects.get(pk=operation['id'])
        self.assertEqual((row.state, row.code), ('refused', 'access_denied'))
        self.assertFalse(self.m.ResourceClaim.objects.filter(released_at=None).exists())

    def test_revoked_executor_is_refused_before_physical_dispatch(self):
        from mop.server.controller.runtime import Runtime, compose_worker
        from django.contrib.auth import get_user_model
        operation = self.submit()
        get_user_model().objects.filter(pk=self.executor.user_id).update(is_active=False)
        async def run():
            runtime = Runtime()
            class Adapter:
                async def set_eligibility(inner, claim):
                    self.fail('revoked executor must not reach physical effect')
            try:
                worker = compose_worker(runtime, node_executor=Adapter(), executor=self.executor)
                await worker.run_one(UUID(operation['id']))
            finally:
                await runtime.aclose()
        asyncio.run(run())
        row = self.m.Operation.objects.get(pk=operation['id'])
        self.assertEqual((row.state, row.code), ('refused', 'access_denied'))
        self.assertFalse(self.m.ResourceClaim.objects.filter(released_at=None).exists())

    def test_pending_filters_capabilities_before_limit_without_stealing_machine_work(self):
        from mop.server.controller.runtime import operations_service
        from mop.server.controller.models import RestoreGuard
        from django.utils import timezone
        from datetime import timedelta
        from uuid import uuid4
        node = self.submit()
        build = self.m.Operation.objects.create(actor=self.user, actor_binding={}, request_id=uuid4(),
            digest='b'*64, epoch=self.epoch, restore_generation=RestoreGuard.objects.get().generation,
            action='build', node=self.node, expected_revision=1, eligible=False,
            not_after=timezone.now()+timedelta(minutes=2))
        self.m.Step.objects.create(operation=build, name='image_build')
        api = operations_service()
        self.assertEqual(api.pending(limit=1, handlers=('image_build',)), (build.pk,))
        self.assertEqual(api.pending(limit=1, handlers=('node_eligibility',)), (UUID(node['id']),))
        self.assertEqual(api.pending(handlers=('node_eligibility',), node_id=uuid4()), ())
        self.assertEqual(api.pending(handlers=('image_build',), node_id=uuid4()), (build.pk,))
        with self.assertRaisesRegex(ValueError, 'handler'):
            api.pending(handlers=('restart_session',))
