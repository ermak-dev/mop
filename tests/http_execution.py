#!/usr/bin/env python3
"""HTTP executor без пула.

HYPOTHESIS: новый node entry отсутствует; старый RPC/marker не даёт HTTP fencing.
SOLUTION: конечный JSON client, runtime-владелец I/O и типизированный local adapter.
RESULT: красный новый entry; отдельно воспроизведены неверный auth header,
отсутствие gate внутри subprocess slot, потерянный child при spawn cancellation,
неотправленный unknown receipt, unbounded shutdown и ложный readiness/restart ACK.
После правок: fake HTTPS, реальные subprocess/Unix socket/tmux проверки зелёные.
STATUS: FIXED — see #640
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import ssl
import shutil
import json
from uuid import uuid4

import hermetic  # noqa: F401
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mop.node.http_execution import MachineClient, Runtime, Work, Refused, Unavailable, HttpsTransport, EffectGuard
from mop.node.execution_adapters import BoundTarget, ActionInput, LocalExecutor, ProcessRunner, UnixSessionSender
from mop.node.execution_facts import LocalFacts, FactScope


def wire(**changes):
    value = dict(schema_version=1, **{k: str(uuid4()) for k in (
        'epoch', 'operation_id', 'step_id', 'target_id', 'incarnation_id', 'node_id',
        'executor_epoch', 'grant_id')}, owner_id='1', generation='1', revision='1',
        role='worker', bundle_digest='a'*64, action='send_message',
        not_after=(datetime.now(timezone.utc)+timedelta(minutes=2)).isoformat())
    value.update(changes)
    return value


class Transport:
    def __init__(self, work):
        self.work = work
        self.calls = []
        self.closed = False
        self.lose = None
        self.claim_change = {}
        self.attempt_id = str(uuid4())

    async def request(self, method, path, payload=None, params=None):
        self.calls.append((method, path, payload, params))
        if path == 'executor/restarted':
            result = dict(schema_version=1, executor_epoch=payload['new_epoch'], replay_allowed=False)
        elif path == 'work' :
            result = dict(schema_version=1, work=[self.work])
        elif path == 'work/claim':
            result = dict(schema_version=1, attempt_id=self.attempt_id, revision='1', state='claimed',
                          lease_until=self.work['not_after'], work={**self.work, **self.claim_change})
        elif path == 'facts':
            result = dict(schema_version=1, node_id=params['node_id'], kind=params['kind'],
                complete=False, items=[dict(target_id=self.work['target_id'],
                incarnation_id=self.work['incarnation_id'], sequence='1',
                observed_at=datetime.now(timezone.utc).isoformat(),
                received_at=datetime.now(timezone.utc).isoformat(), state='unavailable', values={})])
        elif path == 'work/result':
            result = dict(schema_version=1, receipt_id=payload['receipt_id'],
                attempt_id=payload['attempt_id'], revision='2', outcome=payload['outcome'], evidence=payload['evidence'])
        else:
            raise AssertionError(path)
        if self.lose == path:
            raise Unavailable('reply lost')
        return result

    async def aclose(self):
        self.closed = True


class Executor:
    def __init__(self):
        self.calls = []
        self.quiet = True
        self.entered = asyncio.Event()
        self.release = None

    async def reconcile(self, node_id, previous_epoch, new_epoch):
        self.calls.append('reconcile')
        return self.quiet

    async def execute(self, work, guard):
        await guard()
        self.calls.append(work.action)
        self.entered.set()
        if self.release:
            await self.release.wait()
        return 'succeeded', 'delivered'


class ProtocolTests(unittest.TestCase):
    def test_strict_finite_work(self):
        self.assertEqual(Work.parse(wire()).action, 'send_message')
        for change in ({'action':'shell'}, {'owner_id':1}, {'revision':'01'},
                       {'node_id':'not-a-uuid'}, {'not_after':'2030-01-01'},
                       {'schema_version':True}, {'argv':['rm']}, {'role':'../worker'}):
            with self.subTest(change=change), self.assertRaises(Refused):
                Work.parse(wire(**change))


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.value = wire()
        self.transport = Transport(self.value)
        self.client = MachineClient(self.transport)
        self.executor = Executor()
        self.runtime = Runtime(self.client, self.executor, node_id=self.value['node_id'],
            epoch=self.value['epoch'], executor_epoch=self.value['executor_epoch'])
        await self.runtime.start(str(uuid4()))

    async def asyncTearDown(self):
        await self.runtime.aclose()

    async def test_authorized_fact_reads_are_separate_and_unavailable_is_not_empty(self):
        self.executor.release = asyncio.Event()
        await self.runtime.tick()
        await self.executor.entered.wait()
        for kind in ('states','clone','sizes','usage','disk','junk','health'):
            page = await asyncio.wait_for(self.client.facts(self.value['node_id'], kind), .2)
            self.assertFalse(page['complete'])
            self.assertEqual(page['items'][0]['state'], 'unavailable')
        self.transport.lose = 'facts'
        with self.assertRaises(Unavailable): await self.client.facts(self.value['node_id'], 'junk')
        self.executor.release.set()
        await self.runtime.drain()

    async def test_claim_before_effect_then_receipt(self):
        await self.runtime.tick()
        await self.runtime.drain()
        self.assertEqual(self.executor.calls, ['reconcile', 'send_message'])
        self.assertEqual([c[1] for c in self.transport.calls],
            ['executor/restarted','work','work/claim','work/claim','work/result'])
        self.assertEqual(self.transport.calls[-1][2]['outcome'], 'succeeded')

    async def test_foreign_stale_expired_and_incarnation_change_do_not_execute(self):
        original = dict(self.value)
        for change in ({'node_id':str(uuid4())}, {'executor_epoch':str(uuid4())},
                       {'epoch':str(uuid4())}, {'not_after':'2000-01-01T00:00:00+00:00'}):
            self.transport.work = {**original, **change}
            await self.runtime.tick()
            await self.runtime.drain()
        self.assertEqual(self.executor.calls, ['reconcile'])
        self.transport.work = original
        self.transport.claim_change = {'incarnation_id':str(uuid4())}
        await self.runtime.tick()
        await self.runtime.drain()
        self.assertEqual(self.executor.calls, ['reconcile'])

    async def test_unknown_adapter_effect_reports_unknown_and_keeps_conflict_fence(self):
        async def uncertain(work, guard):
            await guard()
            raise Unavailable('tmux reply lost')
        self.executor.execute = uncertain
        await self.runtime.tick()
        await self.runtime.drain()
        self.assertEqual(self.transport.calls[-1][1], 'work/result')
        self.assertEqual(self.transport.calls[-1][2]['outcome'], 'unknown')
        count = len(self.transport.calls)
        await self.runtime.tick()
        await self.runtime.drain()
        self.assertEqual([c[1] for c in self.transport.calls[count:]], ['work'])

    async def test_result_loss_does_not_replay_a_completed_effect(self):
        self.transport.lose = 'work/result'
        await self.runtime.tick()
        await self.runtime.drain()
        self.assertEqual(self.executor.calls.count('send_message'), 1)
        self.transport.lose = None
        for _ in range(3):
            await self.runtime.tick()
            await self.runtime.drain()
        self.assertEqual(self.executor.calls.count('send_message'), 1)

    async def test_lost_claim_or_result_never_replays_effect(self):
        for path in ('work/claim', 'work/result'):
            with self.subTest(path=path):
                self.transport.lose = path
                await self.runtime.tick()
                await self.runtime.drain()
                count = self.executor.calls.count('send_message')
                self.transport.lose = None
                for _ in range(3):
                    await self.runtime.tick()
                    await self.runtime.drain()
                self.assertEqual(self.executor.calls.count('send_message'), count)

    async def test_restart_ack_does_not_prove_old_executor_quiescent(self):
        await self.runtime.aclose()
        ex = Executor()
        ex.quiet = False
        rt = Runtime(MachineClient(self.transport), ex, node_id=self.value['node_id'],
                     epoch=self.value['epoch'], executor_epoch=self.value['executor_epoch'])
        with self.assertRaises(Unavailable):
            await rt.start(str(uuid4()))
        with self.assertRaises(Unavailable):
            await rt.tick()
        await rt.aclose()
        self.assertNotIn('send_message', ex.calls)

    async def test_slow_effect_does_not_block_poll_or_release_conflicting_target(self):
        self.executor.release = asyncio.Event()
        await self.runtime.tick()
        await self.executor.entered.wait()
        await asyncio.wait_for(self.runtime.tick(), .2)
        self.assertEqual(self.executor.calls.count('send_message'), 1)
        self.executor.release.set()
        await self.runtime.drain()

    async def test_shutdown_is_bounded_and_reports_nonquiescent_adapter(self):
        release = asyncio.Event()
        entered = asyncio.Event()
        async def stubborn(work, guard):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return 'unknown', 'effect-running'
        self.executor.execute = stubborn
        self.runtime.shutdown_timeout = .05
        await self.runtime.tick()
        await entered.wait()
        try:
            with self.assertRaises(Unavailable):
                await asyncio.wait_for(self.runtime.aclose(), .3)
            self.assertTrue(self.transport.closed)
        finally:
            release.set()
            await self.runtime.drain()
            # Повторное закрытие не превращает неполный shutdown в успех.
            with self.assertRaises(Unavailable): await self.runtime.aclose()
            self.runtime = Runtime(MachineClient(Transport(self.value)), Executor(),
                node_id=self.value['node_id'], epoch=self.value['epoch'],
                executor_epoch=self.value['executor_epoch'])

    async def test_outage_does_not_cancel_running_session_effect(self):
        self.executor.release = asyncio.Event()
        await self.runtime.tick()
        await self.executor.entered.wait()
        self.transport.lose = 'work'
        with self.assertRaises(Unavailable):
            await self.runtime.tick()
        self.assertEqual(self.executor.calls.count('send_message'), 1)
        self.executor.release.set()
        await self.runtime.drain()


class FactTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_clone_facts_and_missing_sources_never_become_empty_inventory(self):
        work = Work.parse(wire())
        runner = ProcessRunner()
        with tempfile.TemporaryDirectory(prefix='mop640-facts-') as tmp:
            root = Path(tmp)
            await runner.run(('git','init','-q',str(root)))
            target = BoundTarget(work.target_id, work.incarnation_id, work.node_id,
                work.executor_epoch, work.generation, work.role, work.bundle_digest,
                work.grant_id, root, root/'socket', '%0', root/'launcher')
            reads = []
            class Authority:
                async def authorize(self, node_id, kind, limit):
                    reads.append(kind)
                    return FactScope(node_id, kind, (target,), True)
            facts = LocalFacts(runner, Authority())
            clean = await facts.read(node_id=work.node_id, kind='clone', limit=1)
            self.assertTrue(clean['complete'])
            self.assertTrue(clean['items'][0]['values']['clean'])
            (root/'untracked').write_text('untracked')
            dirty = await facts.read(node_id=work.node_id, kind='clone', limit=1)
            self.assertFalse(dirty['items'][0]['values']['clean'])
            disk = await facts.read(node_id=work.node_id, kind='disk', limit=1)
            self.assertGreater(disk['items'][0]['values']['total_bytes'], 0)
            size = await facts.read(node_id=work.node_id, kind='sizes', limit=1)
            self.assertGreater(size['items'][0]['values']['bytes'], 0)
            for kind in ('states','usage','junk'):
                value = await facts.read(node_id=work.node_id, kind=kind, limit=1)
                self.assertFalse(value['complete'])
                self.assertEqual(value['items'][0]['state'], 'unavailable')
                self.assertEqual(value['items'][0]['values'], {})
            denied = LocalFacts(runner, None)
            with self.assertRaises(Unavailable):
                await denied.read(node_id=work.node_id, kind='junk', limit=1)
            self.assertEqual(reads, ['clone','clone','disk','sizes','states','usage','junk'])
        await runner.aclose()


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_tls_fixed_routes_token_auth_and_no_post_retry(self):
        import aiohttp
        for origin in ('http://host.test', 'https://u:p@host.test', 'https://host.test/path'):
            with self.assertRaises(Refused): HttpsTransport(origin, 'credential')
        with self.assertRaises(Refused):
            HttpsTransport('https://host.test', 'credential', tls=ssl._create_unverified_context())
        seen = []
        class Session:
            closed = False
            def __init__(self, **kwargs):
                seen.append(kwargs)
                self.connector = kwargs['connector']
            def request(self, method, url, **kwargs):
                seen.append((method, url, kwargs))
                raise aiohttp.ServerDisconnectedError('lost')
            async def close(self):
                self.closed = True
                await self.connector.close()
        with patch('aiohttp.ClientSession', Session):
            transport = HttpsTransport('https://host.test', 'credential')
            with self.assertRaises(Unavailable):
                await transport.request('POST','work/claim', {'schema_version':1})
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[0]['headers']['Authorization'], 'Token credential')
            self.assertFalse(seen[0]['trust_env'])
            self.assertFalse(seen[1][2]['allow_redirects'])
            with self.assertRaises(Refused): await transport.request('POST','shell')
            await transport.aclose()
            self.assertTrue(transport._session.closed)

    async def test_bounded_json_duplicate_fields_redirect_and_no_raw_error_leak(self):
        class Response:
            status = 200
            content_type = 'application/json'
            body = b'{"schema_version":1}'
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            @property
            def content(self): return self
            async def iter_chunked(self, size):
                yield self.body
        response = Response()
        class Session:
            def __init__(self, **kwargs): self.connector = kwargs['connector']
            def request(self, *args, **kwargs): return response
            async def close(self): await self.connector.close()
        with patch('aiohttp.ClientSession', Session):
            transport = HttpsTransport('https://host.test', 'credential', response_limit=1024)
            self.assertEqual(await transport.request('GET','work'), {'schema_version':1})
            for body in (b'{"x":1,"x":2}', b'no-json', b' '*1025):
                response.body = body
                with self.assertRaises(Refused): await transport.request('GET','work')
            response.status = 302
            with self.assertRaises(Unavailable): await transport.request('GET','work')
            response.status = 403
            with self.assertRaisesRegex(Refused, 'HTTP 403'): await transport.request('GET','work')
            await transport.aclose()


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_unix_send_is_not_terminal_input_or_unproven_success(self):
        work = Work.parse(wire())
        received = asyncio.Future()
        with tempfile.TemporaryDirectory(prefix='mop640-uds-') as tmp:
            root = Path(tmp)
            socket = root/'session.sock'
            async def receive(reader, writer):
                lines = [json.loads(await reader.readline()), json.loads(await reader.readline())]
                received.set_result(lines)
                writer.close()
                await writer.wait_closed()
            server = await asyncio.start_unix_server(receive, path=socket)
            target = BoundTarget(work.target_id, work.incarnation_id, work.node_id,
                work.executor_epoch, work.generation, work.role, work.bundle_digest,
                work.grant_id, root, root/'tmux', '%0', root/'launcher', session_socket=socket)
            inputs = ActionInput('Owner','owner@example.test','text ; $(not-a-shell)', peer_token='private-token')
            sender = UnixSessionSender()
            calls = []
            async def guard(): calls.append('gate')
            try:
                self.assertEqual(await sender.send(target, inputs, guard), ('unknown','effect-running'))
                auth, frame = await asyncio.wait_for(received, .3)
                self.assertEqual(auth, {'type':'auth','token':'private-token'})
                self.assertEqual(frame['type'], 'user')
                self.assertIn(inputs.message, frame['message']['content'])
                self.assertEqual(calls, ['gate'])
            finally:
                await sender.aclose()
                server.close()
                await server.wait_closed()

    async def test_spawn_cancellation_reaps_already_created_child(self):
        runner = ProcessRunner()
        spawned = asyncio.Event()
        release = asyncio.Event()
        original = asyncio.create_subprocess_exec
        children = []
        async def slow_spawn(*args, **kwargs):
            child = await original(*args, **kwargs)
            children.append(child)
            spawned.set()
            await release.wait()
            return child
        try:
            with patch('asyncio.create_subprocess_exec', slow_spawn):
                task = asyncio.create_task(runner.run((sys.executable,'-c','import time;time.sleep(30)')))
                await spawned.wait()
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError): await task
                self.assertIsNotNone(children[0].returncode, 'cancel during spawn leaked child')
                self.assertEqual(runner.active, 0)
        finally:
            for child in children:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            await runner.aclose()

    async def test_gate_runs_inside_subprocess_slot_not_before_wait(self):
        runner = ProcessRunner(concurrency=1)
        await runner._slots.acquire()
        calls = []
        async def gate(): calls.append('gate')
        task = asyncio.create_task(runner.run((sys.executable,'-c','print("ok")'), before_spawn=gate))
        await asyncio.sleep(.01)
        self.assertEqual(calls, [])
        runner._slots.release()
        await task
        self.assertEqual(calls, ['gate'])
        await runner.aclose()

    async def test_real_tmux_disposable_socket_identity_and_restart_not_readiness(self):
        self.assertIsNotNone(shutil.which('tmux'), 'tmux must be declared in test environment')
        runner = ProcessRunner()
        with tempfile.TemporaryDirectory(prefix='mop640-') as tmp:
            root = Path(tmp)
            socket = root/'tmux'
            work = Work.parse(wire(action='apply_identity'))
            await runner.run(('git','init','-q',str(root/'repo')))
            launcher = root/'launcher'
            launcher.write_text('#!/bin/sh\nexec cat\n')
            launcher.chmod(0o700)
            await runner.run(('tmux','-S',str(socket),'new-session','-d','-s','fixture', 'cat'))
            pane = (await runner.run(('tmux','-S',str(socket),'display-message','-p','-t','fixture','#{pane_id}'))).strip()
            target = BoundTarget(work.target_id, work.incarnation_id, work.node_id,
                work.executor_epoch, work.generation, work.role, work.bundle_digest,
                work.grant_id, root/'repo', socket, pane, launcher)
            class Resolver:
                async def resolve(self, value):
                    return target, ActionInput('Fixture','fixture@example.test','hello ; $(no-shell)')
            async def authorize(): pass
            gate = EffectGuard(authorize, lambda: None)
            adapter = LocalExecutor(runner, Resolver())
            try:
                self.assertEqual(await adapter.execute(work, gate), ('succeeded','identity-applied'))
                self.assertEqual((await runner.run(('git','-C',str(root/'repo'),'config','user.email'))).strip(),
                                 'fixture@example.test')
                restart = Work.parse({**work.wire(), 'action':'restart_session'})
                self.assertEqual(await adapter.execute(restart, gate), ('unknown','effect-running'))
                ready = Work.parse({**work.wire(), 'action':'confirm_ready'})
                with self.assertRaises(Unavailable):
                    await adapter.execute(ready, gate)
            finally:
                await runner.run(('tmux','-S',str(socket),'kill-server'))
                await adapter.aclose()

    async def test_missing_physical_binding_fails_closed(self):
        executor = LocalExecutor(ProcessRunner(), None)
        with self.assertRaises(Unavailable):
            await executor.execute(Work.parse(wire()), lambda: asyncio.sleep(0))
        await executor.aclose()

    async def test_real_subprocess_timeout_reaps_child_and_bounds_output(self):
        runner = ProcessRunner(timeout=.08, output_limit=128)
        with self.assertRaises(Unavailable):
            await runner.run((sys.executable, '-c', 'import time;time.sleep(30)'))
        self.assertEqual(runner.active, 0)
        with self.assertRaises(Unavailable):
            await runner.run((sys.executable, '-c', 'print("x"*10000)'))
        self.assertEqual(runner.active, 0)
        self.assertEqual((await runner.run((sys.executable, '-c', 'print("ok")'))).strip(), 'ok')
        await runner.aclose()

    async def test_identity_before_send_with_recheck_between_effects(self):
        work = Work.parse(wire())
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            target = BoundTarget(work.target_id, work.incarnation_id, work.node_id,
                work.executor_epoch, work.generation, work.role, work.bundle_digest,
                work.grant_id, Path(tmp), Path(tmp)/'socket', '%1', Path(tmp)/'launcher')
            class Resolver:
                async def resolve(self, value):
                    self.assertion = value == work
                    return target, ActionInput('Owner', 'owner@example.test', 'hello')
            class Runner:
                async def run(self, argv, **kwargs):
                    await kwargs['before_spawn']()
                    calls.append(tuple(argv))
                    return ''
                async def aclose(self): pass
            async def authorize(): calls.append('gate')
            guard = EffectGuard(authorize, lambda: None)
            class Sender:
                async def send(self, target, inputs, before_send):
                    await before_send()
                    calls.append('message')
                    return 'succeeded', 'delivered'
                async def aclose(self): pass
            adapter = LocalExecutor(Runner(), Resolver(), sender=Sender())
            self.assertEqual(await adapter.execute(work, guard), ('succeeded','delivered'))
            git = [i for i,c in enumerate(calls) if isinstance(c,tuple) and c[0]=='git']
            send = [i for i,c in enumerate(calls) if c == 'message']
            self.assertTrue(git and send)
            self.assertLess(max(git), min(send))
            self.assertEqual(calls[send[0]-1], 'gate')
            await adapter.aclose()


if __name__ == '__main__':
    unittest.main()
