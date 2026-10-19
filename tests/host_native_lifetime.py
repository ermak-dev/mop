#!/usr/bin/env python3
"""HYPOTHESIS: planned broker не удерживает native producer/telemetry lifetime.
SPEC: один настоящий SessionRuntime, producer ACK перед hook, finite mailbox,
original epoch через replacement node frontend; post-switch COMMIT не hint.
SOLUTION: closed body/root/node lifetime composition поверх прежнего runtime.
RESULT: missing lifetime, overlap cleanup, recovery, failure retention и fixed
MCP launch прошли RED -> GREEN. Живой synthetic proc/socket/TTY не actual Claude.
RESULT: actual systemd/Python3.12/PG17/HTTPS fixture запускает native2.1.302,
но fresh onboarding выходит на api.anthropic.com с EAI_AGAIN до root retention.
APPROACH 1 (ABANDONED): Python над shell bin/mop; затем dispatcher с другим
/usr/bin/python3. Теперь manifest Python вызывает hashed isolated body_entry.
STATUS: FIXED — see #760 (supporting checks only; actual native gate остаётся RED)
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

ACTUAL_NATIVE_PROBE = os.environ.get('MOP_HOST_NATIVE_PROBE') == '1'
ACTUAL_NATIVE_BINARY = os.environ.get('MOP_HOST_NATIVE_BINARY')

import _lib  # noqa: F401
import hermetic  # noqa: F401
from _body_fixture import work
from mop.common.producer import ProducerAck
from mop.common.telemetry import Ack, Observation
from mop.driver.launch import RegisteredLayout, Registration
from mop.node.body_launch import SessionRuntime
from mop.node.http_execution import Admission, MachineClient, HttpsTransport, Refused


def api():
    assert importlib.util.find_spec('mop.node.host_native_lifetime') is not None, \
        'root/body/node native lifetime is missing (#760)'
    return importlib.import_module('mop.node.host_native_lifetime')


class LifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hn-', dir='/tmp')
        self.home = Path(self.temp.name)
        self.home.chmod(0o700)
        w = replace(work(), action='native_exec', not_after=datetime.now(timezone.utc)+timedelta(seconds=80))
        self.admission = Admission(w, str(uuid4()), str(uuid4()), w.revision,
            datetime.now(timezone.utc)+timedelta(seconds=75))
        self.registration = Registration(w, str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        self.layout = RegisteredLayout(self.home, self.registration, profile='host-native-v1')
        self.layout.clone.mkdir(mode=0o700)
        self.layout.runtime.mkdir(mode=0o700)
        self.config = self.home/'config'
        self.config.mkdir(mode=0o700)
        self.runtime = None
        self.trace = []

    async def asyncTearDown(self):
        if self.runtime is not None:
            await self.runtime.aclose()
        if self.layout.tmux_socket.exists():
            await asyncio.to_thread(subprocess.run,
                ['tmux', '-S', str(self.layout.tmux_socket), 'kill-server'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        self.temp.cleanup()

    async def native_shaped_process(self):
        fixture = Path(__file__).with_name('_native_observer_fixture.py')
        import sys
        command = shlex.join([sys.executable, '-I', '-u', str(fixture), str(self.layout.runtime/'session.sock')])
        subprocess.run(['tmux', '-S', str(self.layout.tmux_socket), '-f', '/dev/null',
            'new-session', '-d', '-s', 'owned', '-c', str(self.layout.clone), 'exec '+command],
            env={'HOME': str(self.home), 'CLAUDE_CONFIG_DIR': str(self.config), 'PATH': os.defpath,
                 'TERM': 'xterm-256color', 'SHELL': '/bin/sh', 'LC_ALL': 'C.UTF-8'},
            check=True, capture_output=True, timeout=5)
        for _ in range(100):
            if (self.config/'sessions').exists():
                break
            await asyncio.sleep(.02)
        else:
            self.fail('owned synthetic native registry did not start')

    async def test_one_runtime_real_observer_hook_ack_and_replacement_worker(self):
        m = api()
        mailbox = m.NativeMailbox(self.registration, self.admission, desired_model='fixture-desired')
        # Поддерживающая fixture только: root attachment proof проверяется отдельно
        # реальным Systemd adapter; здесь native observer читает реальные proc/socket/TTY.
        class Channel:
            async def request(self, operation, payload):
                return await mailbox.request(operation, payload)
        channel = Channel()
        registration = m.ProxyRegistration(channel, self.registration, self.layout)
        telemetry = m.ProxyTelemetry(channel)
        self.runtime = SessionRuntime(registration, telemetry)
        self.runtime.prepare(self.layout, path=self.layout.hook_socket)
        await self.native_shaped_process()
        observed = []
        async def request(transport, method, path, payload=None, params=None):
            self.trace.append(path)
            if path == 'session-producers':
                observed.append(payload)
                self.assertFalse(self.layout.hook_socket.exists(), 'hook active before producer ACK')
                return ProducerAck(m.ProducerReport.parse({k: payload[k] for k in ('scope', 'launch', 'physical')}).scope, 1).wire()
            if path == 'telemetry':
                row = Observation.parse(payload)
                observed.append(row)
                return dict(schema_version=1, producer_epoch=str(row.scope.producer_epoch), sequence=str(row.sequence))
            self.fail('unexpected HTTP operation '+path)
        # Только transport method test double; production worker требует настоящий
        # MachineClient/HttpsTransport и никогда не передаёт bearer через mailbox.
        transport = object.__new__(HttpsTransport)
        client = MachineClient(transport)
        async def serve_once():
            record = None
            for _ in range(200):
                record = mailbox.poll('first-node')
                if record is not None:
                    break
                await asyncio.sleep(.005)
            self.assertIsNotNone(record)
            answer = await m.execute_proxy(client, record)
            mailbox.complete('first-node', record['id'], record['ticket'], answer)
        with patch.object(HttpsTransport, 'request', request):
            worker = asyncio.create_task(serve_once())
            bridge = await self.runtime.attach()
            await worker
            proof, epoch = self.runtime._proof, self.runtime._epoch
            self.assertEqual(self.trace, ['session-producers'])
            self.assertIsNotNone(mailbox.producer_ack)
            self.assertTrue(bridge.accept(dict(schema_version=1, session_id=proof.scope.session_id,
                event='PostModelSwitch', model='fixture-desired', error=None), token=self.runtime._token))
            flush = asyncio.create_task(bridge.flush())
            for _ in range(200):
                record = mailbox.poll('first-node')
                if record is not None:
                    break
                await asyncio.sleep(.005)
            # Первый frontend потерян после dispatch: новый забирает тот же exact
            # operation, старый ticket уже не может завершить его.
            replacement = mailbox.poll('replacement-node')
            self.assertEqual(record['id'], replacement['id'])
            self.assertEqual(record['payload'], replacement['payload'])
            with self.assertRaises(Refused):
                mailbox.complete('first-node', record['id'], record['ticket'], {})
            answer = await m.execute_proxy(client, replacement)
            mailbox.complete('replacement-node', replacement['id'], replacement['ticket'], answer)
            await flush
            self.assertEqual(mailbox.model_committed.sequence, 1)
            self.assertEqual(mailbox.model_committed.event, 'PostModelSwitch')
            self.assertEqual(mailbox.model_committed.model, 'fixture-desired')
            self.assertEqual(bridge.buffered, 0)
            self.assertEqual(self.runtime._epoch, epoch)
            self.assertEqual(self.runtime._proof, proof)
            self.assertEqual(self.trace, ['session-producers', 'telemetry'])
            self.assertNotIn('bearer', json.dumps(record))
            self.assertEqual(self.runtime._state, 'active')
            # Истёкший исходный phase не отнимает authority у живого producer.
            future = self.admission.work.not_after+timedelta(seconds=1)
            class Expired(datetime):
                @classmethod
                def now(cls, tz=None):
                    return future
            with patch('mop.node.http_execution.datetime', Expired):
                with self.assertRaisesRegex(Refused, 'expired'):
                    self.admission.work.gate(node_id=self.admission.work.node_id,
                        epoch=self.admission.work.epoch, executor_epoch=self.admission.work.executor_epoch)
                self.assertTrue(bridge.accept(dict(schema_version=1, session_id=proof.scope.session_id,
                    event='Stop', model=None, error=None), token=self.runtime._token))
                flush = asyncio.create_task(bridge.flush())
                for _ in range(200):
                    record = mailbox.poll('expired-phase-node')
                    if record is not None:
                        break
                    await asyncio.sleep(.005)
                self.assertIsNotNone(record)
                answer = await m.execute_proxy(client, record)
                mailbox.complete('expired-phase-node', record['id'], record['ticket'], answer)
                await flush
            self.assertEqual(mailbox.last_observation.sequence, 2)
            self.assertEqual(mailbox.model_committed.sequence, 1)
            self.assertEqual(self.runtime._epoch, epoch)
            # Второй body caller не становится cleanup owner первой записи.
            row = replace(mailbox.model_committed, sequence=3, event='Stop', model=None)
            original = asyncio.create_task(mailbox.request('ingest_telemetry', row.wire()))
            await asyncio.sleep(0)
            try:
                async with asyncio.timeout(.05):
                    with self.assertRaises(Refused):
                        await mailbox.request('ingest_telemetry', row.wire())
                self.assertEqual(mailbox.pending, 1)
                self.assertFalse(original.done())
            finally:
                original.cancel()
                await asyncio.gather(original, return_exceptions=True)
            self.assertEqual(mailbox.pending, 0)
            # Lifetime наблюдается и без node frontend/новых hook сообщений.
            from types import SimpleNamespace
            from mop.node.host_native_body import NativeBody
            from mop.node.http_execution import Unavailable
            owner = NativeBody.__new__(NativeBody)
            owner.runtime = self.runtime
            reader = asyncio.create_task(asyncio.Event().wait())
            owner.channel = SimpleNamespace(reader_task=reader)
            await asyncio.to_thread(subprocess.run,
                ['tmux', '-S', str(self.layout.tmux_socket), 'kill-server'],
                capture_output=True, check=True, timeout=5)
            try:
                async with asyncio.timeout(2):
                    with self.assertRaises(Unavailable):
                        await owner.retain()
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

    async def test_mailbox_is_bounded_and_finite_before_producer_ack(self):
        m = api()
        box = m.NativeMailbox(self.registration, self.admission, desired_model='fixture-desired')
        self.assertEqual(box.capacity, 4)
        with self.assertRaises(Refused):
            await box.request('arbitrary-http', {'path': '/secrets'})
        with self.assertRaises(Refused):
            await box.request('ingest_telemetry', {})
        self.assertEqual(box.pending, 0)

    async def test_root_os_refusal_keeps_fixed_stage_without_arbitrary_path(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from mop.node.host_native_owner import NativeOwner
        root = SimpleNamespace(_peer=AsyncMock(side_effect=FileNotFoundError(2, 'raw secret message', '/secret')))
        owner = NativeOwner(root, None, None, None)
        owner.reply = AsyncMock()
        identifier = str(uuid4())
        await owner.request(dict(id=identifier, operation='native_launched', payload={}))
        owner.reply.assert_awaited_once_with(identifier,
            error='native root peer failed: FileNotFoundError')

    async def test_original_body_failure_survives_mainpid_exit(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from mop.node.host_native_owner import NativeOwner
        from mop.node.host_scope import Unknown, encode
        reader = asyncio.StreamReader()
        reader.feed_data(encode(dict(schema_version=1, action='body_failed',
            attempt_id=self.admission.attempt_id, error='original fixed native launch refusal'))+b'\n')
        reader.feed_eof()
        root = SimpleNamespace(_peer=AsyncMock(side_effect=Unknown('MainPID already exited')),
            _phase_cleanup_task=Mock())
        body = SimpleNamespace(streams=(reader, None, None))
        owner = NativeOwner(root, body, SimpleNamespace(submission=SimpleNamespace(admission=self.admission)), None)
        await owner.run()
        self.assertEqual(owner.error, 'original fixed native launch refusal')
        root._phase_cleanup_task.assert_called_once_with(body)

    async def test_fixed_native_launch_uses_the_isolated_python_mcp_entry(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from mop.node.host_native_body import NativeBody
        # bin/mop — shell dispatcher с /usr/bin/python3. Native MCP обязан
        # использовать manifest Python и hashed fixed body_entry, не shell bytes
        # и не другой interpreter из PATH/dispatcher.
        self.layout.runtime.rmdir()
        tools = {key: Path('/fixed')/key for key in ('python', 'native', 'tmux', 'git', 'hook')}
        tools['mcp'] = Path('/fixed/release/bin/mop')
        broker = SimpleNamespace(manifest=SimpleNamespace(tools=tools, require_native_release=Mock()),
            config=SimpleNamespace(installed=SimpleNamespace(runner=Path('/fixed/release/mop/node/host_body.py'))),
            scope=SimpleNamespace(unit='owned.service'), write=lambda path, data: path.write_bytes(data))
        owner = NativeBody.__new__(NativeBody)
        owner.started, owner.broker, owner.layout = False, broker, self.layout
        owner.submission = SimpleNamespace(admission=self.admission)
        owner.inputs = SimpleNamespace(bootstrap_model='fixture-bootstrap', environment=Mock(return_value={}))
        owner.profile = None
        owner.runtime = SimpleNamespace(prepare=Mock(return_value={}))
        owner.checkpoint = AsyncMock()
        owner.tmux = AsyncMock(return_value=f'{os.getpid()}|{os.getpid()}'.encode())
        owner.channel = SimpleNamespace(request=AsyncMock(return_value={'retained': True}))
        with patch('os.readlink', return_value=str(tools['native'])):
            await owner.launch()
        command = shlex.split(owner.tmux.await_args_list[0].args[-1])[1:]
        config = json.loads(command[-1])['mcpServers']['mop']
        self.assertEqual(config['command'], str(tools['python']))
        self.assertEqual(config['args'], ['-I', '-S', '-B', '/fixed/release/mop/client/body_entry.py',
            '--scope', broker.scope.unit, '--deadline', owner.window.deadline.isoformat()])
        self.assertNotIn('-p', command)
        self.assertNotIn('--bare', command)

    async def test_root_native_graph_imports_without_site_packages(self):
        import sys
        source = Path(__file__).resolve().parents[1]
        result = await asyncio.to_thread(subprocess.run, [sys.executable, '-ISB', '-c',
            'import sys;sys.path.insert(0,sys.argv[1]);from mop.node.host_native_owner import submit_native;from mop.node.host_native_body import installed_profile', str(source)],
            capture_output=True, text=True, env={'PATH': os.defpath}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    async def test_native_result_recovery_uses_standard_executor_entry(self):
        from unittest.mock import AsyncMock
        from mop.node.host_native_client import InitialHostExecutor
        executor = InitialHostExecutor.__new__(InitialHostExecutor)
        executor.recover_native_result = AsyncMock()
        await executor.recover_result(self.admission)
        executor.recover_native_result.assert_awaited_once_with(self.admission)
        with self.assertRaisesRegex(Refused, 'Admission'):
            await executor.recover_result(None)

    async def test_root_native_owner_and_node_worker_are_bound_to_existing_broker(self):
        from mop.node.host_phase_supervisor import PhaseOwner, Body
        owner = PhaseOwner.__new__(PhaseOwner)
        self.assertTrue(callable(getattr(owner, 'submit_native', None)),
            'root phase owner cannot accept and retain actual native lifetime')
        body = Body(None, None)
        self.assertIsNone(body.native)
        from mop.node import host_backend_client
        self.assertTrue(callable(getattr(host_backend_client._Session, 'native_mailbox', None)),
            'node cannot drain finite lifetime mailbox independently of phase guard')

    @unittest.skipUnless(ACTUAL_NATIVE_PROBE, 'owned actual native fixture is opt-in')
    async def test_actual_native_root_body_node_producer_and_model_commit(self):
        self.assertIsNotNone(importlib.util.find_spec('mop.node.host_native_probe'),
            'actual immutable native root/body/node fixture is missing (#760)')
        probe = importlib.import_module('mop.node.host_native_probe')
        self.assertIsNotNone(ACTUAL_NATIVE_BINARY, 'explicit owned native binary fixture input required')
        result = await probe.run_probe(Path(__file__).resolve().parents[1], native=ACTUAL_NATIVE_BINARY)
        self.assertEqual(result['status'], 'accepted')
        self.assertTrue(result['native_execution'])
        self.assertTrue(result['post_ack_model_commit'])
        self.assertTrue(result['original_lifetime_after_frontend_exit'])
        self.assertFalse(result['production_rbac'])
        self.assertFalse(result['ready_confirmation'])

    async def test_native_body_is_fixed_launch_not_external_attach_callback(self):
        m = api()
        # Реальный normal native запускается fixed body runner, не test lambda.
        body_module = importlib.import_module('mop.node.host_native_body')
        self.assertTrue(callable(getattr(body_module.NativeBody, 'run', None)))
        self.assertTrue(callable(getattr(body_module.NativeBody, 'launch', None)))
        self.assertFalse(hasattr(m, 'NativeBindings'))


if __name__ == '__main__':
    unittest.main()
