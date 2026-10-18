#!/usr/bin/env python3
"""HYPOTHESIS: attach выдаёт hook env после exec, ребёнок его не наследует.
SPEC: prepare -> native exec -> exact producer ACK -> scoped bridge;
startup hook теряется, producer epoch принадлежит одному explicit attachment.
SOLUTION: pre-exec ephemeral env и explicit native attachment; owner только lookup,
ACK и повторный physical gate перед activation/ingest, independently owned cleanup.
RESULT: сначала 11 RED на отсутствии prepare; отдельно RED на unbounded первом
snapshot. Затем 14 GREEN: env реального child, все поля scope ACK, pending ACK,
смена conversation и настоящего процесса, dropped startup, cancel/start/close.
Настоящие tmux/Unix/proc и stdlib hook, synthetic native registry/key fixture;
не actual CLI acceptance, model readiness или physical retirement.
STATUS: FIXED — see #643 (pre-exec lifetime; root/work-step wiring отдельно).
"""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import _lib  # noqa: F401
import hermetic
from _body_fixture import work
from mop.common.telemetry import Ack
from mop.driver.launch import RegisteredLayout, Registration
from mop.node.body_launch import SessionRuntime
from mop.node.execution_adapters import process_identity
from mop.node.http_execution import Refused, Unavailable
from mop.node.native_observer import NativePhysicalSessions, _snapshot


class Source:
    def __init__(self):
        self.registered = []
        self.rows = []
        self.ack = lambda proof: proof.scope
        self.registering = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def register(self, proof):
        self.registered.append(proof)
        self.registering.set()
        await self.release.wait()
        result = self.ack(proof)
        if isinstance(result, BaseException):
            raise result
        return result

    async def ingest(self, row):
        self.rows.append(row)
        return Ack(row.scope.producer_epoch, row.sequence)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if not shutil.which('tmux'):
            hermetic.skip('session runtime', 'tmux is required')
            self.skipTest('tmux is required')
        self.directory = tempfile.TemporaryDirectory(prefix='m643h-', dir='/tmp')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.root.chmod(0o700)
        self.registration = Registration(work(), str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        self.layout = RegisteredLayout(self.root, self.registration)
        self.layout.clone.mkdir(parents=True)
        self.layout.runtime.mkdir(parents=True, mode=0o700)
        self.config = self.root/'native-config'
        self.config.mkdir()
        self.path = self.layout.runtime/'hook.sock'
        self.base = ['tmux', '-S', str(self.layout.tmux_socket)]
        self.source = Source()
        self.runtime = SessionRuntime(self.source, self.source)
        self.addAsyncCleanup(self.runtime.aclose)
        self.pid = None
        self.addCleanup(self.stop)
        # Чёткий RED до любых child effects, не ImportError отсутствующего API.
        self.assertTrue(callable(getattr(self.runtime, 'prepare', None)),
                        'hook environment must be prepared before native exec')

    def prepare(self):
        return self.runtime.prepare(self.layout, path=self.path)

    async def wait_file(self, path):
        async with asyncio.timeout(4):
            while not path.exists():
                await asyncio.sleep(.01)

    async def spawn(self, env):
        fixture = Path(__file__).with_name('_native_observer_fixture.py')
        hook = Path(__file__).resolve().parents[1]/'mop/session.py'
        environment = dict(HOME=str(self.root), CLAUDE_CONFIG_DIR=str(self.config),
            PATH=os.defpath, TERM='xterm-256color', SHELL='/bin/sh', LC_ALL='C.UTF-8', **env)
        argv = [sys.executable, '-I', '-u', str(fixture), str(self.layout.runtime/'session.sock'), str(hook)]
        result = subprocess.run([*self.base, '-f', '/dev/null', 'new-session', '-d',
            '-s', 'owned', '-c', str(self.layout.clone), 'exec '+shlex.join(argv)],
            env=environment, capture_output=True, text=True, timeout=3)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.record_path = self.config/'sessions/owned.json'
        await self.wait_file(self.record_path)
        self.record = json.loads(self.record_path.read_text())
        self.pid = self.record['pid']
        self.start = process_identity(self.pid)
        self.assertEqual(json.loads((self.config/'hook-startup').read_text()), [0, '', ''])

    def stop(self):
        subprocess.run([*self.base, 'kill-server'], capture_output=True, timeout=3)
        if self.pid is None:
            return
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            try:
                if process_identity(self.pid) != self.start:
                    return
            except Unavailable:
                return
            time.sleep(.01)
        raise AssertionError('owned native-shaped fixture survived cleanup')

    async def hook(self):
        marker = self.config/'hook-later'
        marker.unlink(missing_ok=True)
        os.kill(self.pid, signal.SIGWINCH)
        await self.wait_file(marker)
        self.assertEqual(json.loads(marker.read_text()), [0, '', ''])
        # Запись прошла отдельный child; дождаться bounded handler, не worker tick.
        bridge = self.runtime._bridge
        if bridge is not None:
            async with asyncio.timeout(2):
                while bridge._handlers:
                    await asyncio.sleep(.001)

    async def assert_closed(self, bridge=None):
        await self.runtime.aclose()
        self.assertFalse(self.path.exists())
        if bridge is not None:
            self.assertEqual(bridge.buffered, 0)
            self.assertFalse(bridge.pending_tasks)
        with self.assertRaises(Unavailable):
            self.runtime.environment()
        with self.assertRaises(Unavailable):
            await self.runtime.attach()

    async def test_preexec_environment_is_inherited_and_startup_is_dropped_not_replayed(self):
        env = self.prepare()
        self.assertEqual(set(env), {'MOP_TELEMETRY_SOCKET', 'MOP_TELEMETRY_TOKEN'})
        self.assertEqual(len(env['MOP_TELEMETRY_TOKEN']), 64)
        self.assertEqual(env['MOP_TELEMETRY_SOCKET'], str(self.path))
        self.assertFalse(env['MOP_TELEMETRY_TOKEN'] in repr(self.runtime),
                         'runtime repr must omit the ephemeral credential')
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.layout.runtime.iterdir()), [], 'prepare must not persist authority/token')
        await self.spawn(env)
        self.assertEqual(self.source.registered, [])
        self.assertEqual(self.source.rows, [])
        # Без изменения parent environ/env child после exec: повторный hook
        # producer обязан воспользоваться ровно ранее унаследованным credential.
        bridge = await self.runtime.attach()
        self.assertTrue(bridge.environment() == env, 'activation must preserve the pre-exec credential')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(bridge.buffered, 0, 'startup hook was replayed as readiness')
        await self.hook()
        await bridge.flush()
        self.assertEqual(len(self.source.rows), 1)
        proof = self.source.registered[0]
        self.assertEqual(self.source.rows[0].scope, proof.scope)
        self.assertEqual(self.source.rows[0].sequence, 1)
        self.assertNotEqual(proof.producer_epoch, UUID(self.registration.work.executor_epoch))
        observer = NativePhysicalSessions(owner=self.runtime)
        self.assertEqual(await observer.observe(self.layout), proof)
        self.assertEqual(await observer.observe(self.layout), proof)
        self.assertFalse(getattr(observer, 'supports_launch_scope', False))
        self.assertFalse(getattr(observer, 'scope_retirement_v1', False))
        await self.assert_closed(bridge)
        # Producer жив; закрытие bridge не объявляет физическое завершение CLI.
        self.assertEqual(process_identity(self.pid), self.start)
        await self.hook()
        self.assertEqual(len(self.source.rows), 1)

    async def test_prepare_and_observe_cannot_attach_or_mint_a_producer(self):
        env = self.prepare()
        await self.spawn(env)
        identity = await asyncio.to_thread(_snapshot, self.layout)
        self.assertIsNone(await self.runtime.producer_epoch(self.layout, identity))
        for _ in range(2):
            with self.assertRaisesRegex(Unavailable, 'producer|owner'):
                await NativePhysicalSessions(owner=self.runtime).observe(self.layout)
        self.assertEqual(self.source.registered, [])
        self.assertFalse(self.path.exists())
        await self.runtime.attach()
        self.assertEqual(len(self.source.registered), 1)
        with self.assertRaises(Unavailable):
            self.prepare()
        with self.assertRaises(Unavailable):
            await self.runtime.attach()

    async def test_missing_mismatched_and_failed_ack_cannot_create_a_usable_bridge(self):
        mismatches = [lambda p, name=name: replace(p.scope, **{name: uuid4()})
                      for name in ('epoch', 'target_id', 'incarnation_id', 'node_id',
                                   'executor_epoch', 'producer_epoch')]
        for ack in (lambda p: None, *mismatches,
                    lambda p: replace(p.scope, session_id='foreign-session'),
                    lambda p: Unavailable('fixture registration refused')):
            with self.subTest(ack=ack):
                runtime = SessionRuntime(self.source, self.source)
                env = runtime.prepare(self.layout, path=self.path)
                if self.pid is None:
                    await self.spawn(env)
                self.source.ack = ack
                with self.assertRaises((Refused, Unavailable)):
                    await runtime.attach()
                self.assertFalse(self.path.exists())
                await runtime.aclose()
        self.assertEqual(self.source.rows, [])

    async def test_pending_ack_has_no_bridge_and_cancellation_cleans_up(self):
        env = self.prepare()
        await self.spawn(env)
        self.source.release.clear()
        task = asyncio.create_task(self.runtime.attach())
        try:
            await asyncio.wait_for(self.source.registering.wait(), 2)
            await self.hook()
            self.assertFalse(self.path.exists())
            self.assertEqual(self.source.rows, [])
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await self.assert_closed()
        finally:
            self.source.release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_close_during_registration_is_terminal_even_if_ack_later_succeeds(self):
        env = self.prepare()
        await self.spawn(env)
        self.source.release.clear()
        task = asyncio.create_task(self.runtime.attach())
        try:
            await asyncio.wait_for(self.source.registering.wait(), 2)
            await asyncio.wait_for(self.runtime.aclose(), 1)
            self.source.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await self.assert_closed()
            self.assertEqual(self.source.rows, [])
        finally:
            self.source.release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_context_change_during_ack_cannot_activate_old_hook(self):
        env = self.prepare()
        await self.spawn(env)
        self.source.release.clear()
        task = asyncio.create_task(self.runtime.attach())
        try:
            await asyncio.wait_for(self.source.registering.wait(), 2)
            self.record_path.write_text(json.dumps({**self.record, 'sessionId':'after-clear'}))
            self.source.release.set()
            with self.assertRaises((Refused, Unavailable)):
                await task
            await self.assert_closed()
            await self.hook()
            self.assertEqual(self.source.rows, [])
        finally:
            self.source.release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_context_change_invalidates_queued_and_future_old_producer_hooks(self):
        env = self.prepare()
        await self.spawn(env)
        bridge = await self.runtime.attach()
        # Остановить только periodic sender, чтобы old row гарантированно стояла
        # в памяти при /clear. Реальный manual flush обязан пройти тот же gate.
        bridge._worker.cancel()
        await asyncio.gather(bridge._worker, return_exceptions=True)
        await self.hook()
        self.assertEqual(bridge.buffered, 1)
        self.record_path.write_text(json.dumps({**self.record, 'sessionId':'after-clear'}))
        await bridge.flush()
        self.assertEqual(self.source.rows, [], 'old queued scope reached ingest after context change')
        async with asyncio.timeout(2):
            while self.path.exists() or bridge.pending_tasks:
                await asyncio.sleep(.001)
        await self.assert_closed(bridge)
        await self.hook()
        self.assertEqual(self.source.rows, [])

    async def test_changed_native_producer_rejects_old_hook_even_with_same_session_id(self):
        env = self.prepare()
        await self.spawn(env)
        bridge = await self.runtime.attach()
        identity = await asyncio.to_thread(_snapshot, self.layout)
        self.assertIsNone(await self.runtime.producer_epoch(self.layout,
            replace(identity, process_start='replacement-boot:start')))
        await self.assert_closed(bridge)
        await self.hook()
        self.assertEqual(self.source.rows, [])

    async def test_real_replacement_cannot_reuse_the_old_environment_as_a_new_producer(self):
        env = self.prepare()
        await self.spawn(env)
        bridge = await self.runtime.attach()
        old_start, old_pid = self.start, self.pid
        self.stop()
        self.pid = None
        self.record_path.unlink()
        (self.config/'hook-startup').unlink()
        (self.layout.runtime/'session.sock').unlink()
        # Ошибочный root повторно передал прежний env новому exec. Реальная
        # замена PID/start при том же session_id/socket/token не новый ACK.
        await self.spawn(env)
        self.assertNotEqual((self.pid, self.start), (old_pid, old_start))
        await self.hook()
        await bridge.flush()
        async with asyncio.timeout(2):
            while self.path.exists() or bridge.pending_tasks:
                await asyncio.sleep(.001)
        self.assertEqual(self.source.rows, [])
        self.assertEqual(len(self.source.registered), 1)
        await self.assert_closed(bridge)

    async def test_exact_work_registration_is_not_weakened_for_another_operation_or_step(self):
        env = self.prepare()
        await self.spawn(env)
        bridge = await self.runtime.attach()
        proof = self.source.registered[0]
        for name in ('operation_id', 'step_id', 'executor_epoch', 'incarnation_id', 'grant_id'):
            other = replace(self.registration.work, **{name: str(uuid4())})
            self.assertFalse(self.registration.matches(other))
            layout = replace(self.layout, registration=replace(self.registration, work=other))
            with self.subTest(field=name), self.assertRaises(Unavailable):
                await NativePhysicalSessions(owner=self.runtime).observe(layout)
        self.assertEqual(await NativePhysicalSessions(owner=self.runtime).observe(self.layout), proof)
        await self.hook()
        await bridge.flush()
        self.assertEqual(self.source.rows[0].scope, proof.scope)

    async def test_failed_bridge_start_and_cancelled_start_remove_only_owned_endpoint(self):
        from mop.node.telemetry import HookBridge
        env = self.prepare()
        await self.spawn(env)
        started = asyncio.Event()
        original = HookBridge.start
        async def stalled(bridge):
            await original(bridge)
            started.set()
            await asyncio.Event().wait()
        with patch.object(HookBridge, 'start', stalled):
            task = asyncio.create_task(self.runtime.attach())
            try:
                await asyncio.wait_for(started.wait(), 2)
                await self.hook()
                self.assertEqual(self.runtime._bridge.buffered, 0,
                                 'a visible listener is not an activated producer')
                self.assertEqual(self.source.rows, [])
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await self.assert_closed(self.runtime._bridge)
            finally:
                await asyncio.gather(task, return_exceptions=True)
        # Existing foreign pathname remains untouched by failed start/cleanup.
        runtime = SessionRuntime(self.source, self.source)
        runtime.prepare(self.layout, path=self.path)
        self.path.write_text('foreign endpoint fixture')
        try:
            with self.assertRaisesRegex(ValueError, 'exists'):
                await runtime.attach()
            self.assertEqual(self.path.read_text(), 'foreign endpoint fixture')
        finally:
            await runtime.aclose()
            self.path.unlink()

    async def test_repeated_close_cancellation_keeps_socket_cleanup_owned(self):
        env = self.prepare()
        await self.spawn(env)
        bridge = await self.runtime.attach()
        entered, release = asyncio.Event(), asyncio.Event()
        original = bridge.aclose
        async def stalled():
            entered.set()
            await release.wait()
            await original()
        with patch.object(bridge, 'aclose', stalled):
            closer = asyncio.create_task(self.runtime.aclose())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                closer.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await closer
                for _ in range(2):
                    closer = asyncio.create_task(self.runtime.aclose())
                    await asyncio.sleep(0)
                    closer.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await closer
                self.assertTrue(self.path.exists(), 'fixture cleanup has not yet been released')
                await self.hook()
                self.assertEqual(self.source.rows, [], 'closed runtime accepted a hook during cleanup')
                release.set()
                await self.assert_closed(bridge)
            finally:
                release.set()
                await asyncio.gather(closer, return_exceptions=True)
                await self.runtime.aclose()

    async def test_initial_native_snapshot_has_a_finite_budget(self):
        # HYPOTHESIS: lifecycle обходит bounded observe при первом snapshot;
        # зависший OS read не должен удерживать attach/credential без срока.
        import threading
        release = threading.Event()
        self.prepare()
        def stalled(layout):
            release.wait(8)
        with patch('mop.node.native_observer._snapshot', stalled):
            task = asyncio.create_task(self.runtime.attach())
            try:
                done, _ = await asyncio.wait({task}, timeout=5.3)
                self.assertIn(task, done, 'initial native snapshot escaped its five-second budget')
                with self.assertRaises(TimeoutError):
                    await task
                await self.assert_closed()
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_prepare_requires_private_owned_bounded_path_before_any_child_effect(self):
        for path in (Path('relative'), self.root/('x'*110), self.root/'missing/hook.sock'):
            runtime = SessionRuntime(self.source, self.source)
            with self.subTest(path=path), self.assertRaises((ValueError, Refused, OSError)):
                runtime.prepare(self.layout, path=path)
            await runtime.aclose()
        public = self.root/'public'
        public.mkdir(mode=0o755)
        linked = self.root/'link'
        linked.symlink_to(self.layout.runtime)
        for path in (public/'hook.sock', linked/'hook.sock'):
            runtime = SessionRuntime(self.source, self.source)
            with self.assertRaises((ValueError, Refused, OSError)):
                runtime.prepare(self.layout, path=path)
            await runtime.aclose()
        self.assertEqual(self.source.registered, [])


if __name__ == '__main__':
    unittest.main()
