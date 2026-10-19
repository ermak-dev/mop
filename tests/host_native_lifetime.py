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
        from types import SimpleNamespace
        from mop.node.host_native_client import LifetimeWorker
        from mop.node.host_native_owner import mailbox_exchange
        from mop.node.host_scope import Scope
        w = self.registration.work
        scope = Scope(w.epoch, w.node_id, w.executor_epoch, w.target_id, w.incarnation_id, w.operation_id)
        frontends = {name: LifetimeWorker(client, None, scope, self.admission.attempt_id)
            for name in ('first-node', 'replacement-node', 'expired-phase-node')}
        item = SimpleNamespace()
        # Только protocol seam; real proc/socket/TTY observer ниже не подменяется.
        async def check():
            if mailbox.closed: raise Refused('original mailbox closed')
        life = SimpleNamespace(item=item, mailbox=mailbox, check=check)
        root = SimpleNamespace(_attempt=lambda *args: item, bodies={scope: SimpleNamespace(native=life, cleanup=None)})
        for name, frontend in frontends.items():
            async def exchange(request, process=name):
                return await mailbox_exchange(root, request, process)
            frontend.exchange = exchange
        def peer(name): return name+':'+frontends[name].frontend
        async def dispatch(name, record):
            frontend = frontends[name]
            frontend.current = record
            try:
                return await m.execute_proxy(client, record, worker=frontend)
            finally:
                frontend.current = None
        async def serve_once():
            record = None
            for _ in range(200):
                record = mailbox.poll(peer('first-node'))
                if record is not None:
                    break
                await asyncio.sleep(.005)
            self.assertIsNotNone(record)
            answer = await dispatch('first-node', record)
            mailbox.complete(peer('first-node'), record['id'], record['ticket'], answer)
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
                record = mailbox.poll(peer('first-node'))
                if record is not None:
                    break
                await asyncio.sleep(.005)
            # Первый frontend потерян после poll, но до HTTP: новый забирает тот
            # же exact operation, старый ticket уже не может завершить его.
            replacement = mailbox.poll(peer('replacement-node'))
            self.assertEqual(record['id'], replacement['id'])
            self.assertEqual(record['payload'], replacement['payload'])
            with self.assertRaises(Refused):
                mailbox.complete(peer('first-node'), record['id'], record['ticket'], {})
            answer = await dispatch('replacement-node', replacement)
            mailbox.complete(peer('replacement-node'), replacement['id'], replacement['ticket'], answer)
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
                    record = mailbox.poll(peer('expired-phase-node'))
                    if record is not None:
                        break
                    await asyncio.sleep(.005)
                self.assertIsNotNone(record)
                answer = await dispatch('expired-phase-node', record)
                mailbox.complete(peer('expired-phase-node'), record['id'], record['ticket'], answer)
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


# BUG #781: complete populated status, а не короткая projection, превышает frame.
# HYPOTHESIS: Reference и SessionScope повторяются в native proof/ACK/observation.
# SOLUTION: замкнутая shared-v1 projection + whole-frame preflight до snapshot/effect.
# RESULT: populated status/result ACK проходят через actual handler и Node decoder.
# STATUS: FIXED — see #781
class PopulatedStatusTests(unittest.IsolatedAsyncioTestCase):
    def populated(self, *, bounded=False, escaped=False):
        from types import SimpleNamespace
        from uuid import UUID
        from host_native import model_delivery
        from mop.common.telemetry import SessionScope
        from mop.node.host_backend import Process
        from mop.node.host_native_owner import NativeOwner
        from mop.node.host_phase_manifest import Capacity, path
        from mop.node.host_phase_supervisor import PhaseOwner, Attempt, Body
        from mop.node.host_phases import Projection
        from mop.node.host_scope import Reference
        a, scope, delivery = model_delivery()
        if bounded:
            w = replace(a.work, owner_id=2**62, generation=2**62, revision=2**62)
            a = replace(a, work=w, revision=2**62)
        reference = Reference(scope, uuid4().hex, str(uuid4()))
        r = Registration(a.work, delivery['bootstrap_revision'], delivery['bootstrap_digest'],
            delivery['image_digest'], delivery['display_name'])
        session = SessionScope(*(UUID(getattr(a.work, k)) for k in
            ('epoch', 'target_id', 'incarnation_id', 'node_id', 'executor_epoch')), str(uuid4()), uuid4())
        box = api().NativeMailbox(r, a, desired_model='m'*128 if bounded else 'fixture-desired')
        box.producer_ack = ProducerAck(session, 1)
        box.model_committed = Observation(session, 2**63-1 if bounded else 1,
            'PostModelSwitch', None, box.desired_model, None, None, 0)
        body = Body(scope, Projection.from_delivery(delivery), reference=reference)
        item = Attempt(SimpleNamespace(admission=a, descriptor=SimpleNamespace(scope=scope)),
            None, 1e12, status='succeeded')
        body.native = NativeOwner(None, body, item, box)
        native = '/opt/'+'n'*235 if bounded else '/opt/runtime/native'
        tmux = '/opt/'+'t'*235 if bounded else '/usr/bin/tmux'
        if escaped:
            native, tmux = '/opt/'+'\"'*235, '/opt/'+'\\'*235
        body.native.physical = dict(reference=reference.wire(),
            runner=Process(12344, 12345677, reference.boot_id).wire(), session_id=session.session_id,
            native=dict(process=Process(12345, 12345678, reference.boot_id).wire(), executable=str(path(native))),
            tmux=dict(process=Process(12346, 12345679, reference.boot_id).wire(), executable=str(path(tmux))))
        root = PhaseOwner.__new__(PhaseOwner)
        root.bodies = {scope: body}
        root._attempt = lambda *args: item
        capacity = Capacity(4000, 100, 128, 8192, 8000, 'shared-reservation')
        root.manifest = SimpleNamespace(content_digest='a'*64,
            tools=dict(native=path(native), tmux=path(tmux)), project=lambda p: capacity.project(p.image_policy))
        return root, body, item

    async def status_exchange(self, root, item, *, ack=False):
        from mop.node.host_scope import encode, decode
        scope = item.submission.descriptor.scope
        request = dict(schema_version=1, action='phase_status', scope=scope.wire(),
            attempt_id=item.submission.admission.attempt_id)
        if ack:
            a = item.submission.admission
            request.update(action='phase_ack', receipt=item.phase_receipt,
                committed=dict(receipt_id=item.phase_receipt, attempt_id=a.attempt_id,
                    revision=str(a.revision+1), outcome='succeeded', evidence='native-started'))
        reader = asyncio.StreamReader()
        reader.feed_data(encode(dict(schema_version=1, action='phase_response_ack'))+b'\n')
        class Writer:
            def __init__(self): self.frames = []
            def write(self, raw): self.frames.append(raw)
            async def drain(self): pass
        writer = Writer()
        try:
            await root.handle_planned(reader, writer, encode(request))
        except Refused as error:
            raw = json.dumps(dict(schema_version=1, status='ok', phase=root.status(item), error=None),
                separators=(',', ':'), ensure_ascii=True).encode()
            self.fail(f'{error}: {len(raw)} bytes, {len(writer.frames)} frames written')
        self.assertEqual(len(writer.frames), 1)
        self.assertLessEqual(len(writer.frames[0])-1, 4096)
        response = decode(writer.frames[0][:-1])
        self.assertEqual(response['status'], 'ok')
        return response, len(writer.frames[0])-1

    async def test_populated_short_status_and_ack_fit_complete_frames(self):
        root, body, item = self.populated()
        response, size = await self.status_exchange(root, item)
        self.assertEqual(response['phase']['receipt'], item.phase_receipt)
        response, _ = await self.status_exchange(root, item, ack=True)
        self.assertTrue(response['phase']['acknowledged'])
        self.assertEqual(body.completed, ['native_exec'])

    async def test_schema_valid_populated_status_at_legal_paths_models_and_counters(self):
        root, body, item = self.populated(bounded=True)
        response, size = await self.status_exchange(root, item)
        self.assertEqual(response['phase']['reference'], body.reference.wire())
        await self.status_exchange(root, item, ack=True)

    async def test_json_escaping_is_counted_in_complete_populated_frame(self):
        root, body, item = self.populated(bounded=True, escaped=True)
        await self.status_exchange(root, item)

    async def test_node_decodes_exact_proof_and_ack_recovery_without_cached_identity(self):
        from types import SimpleNamespace
        from mop.node.host_backend_client import _Session
        from mop.node.host_scope import encode
        root, body, item = self.populated(bounded=True)
        response, size = await self.status_exchange(root, item)
        reader = asyncio.StreamReader()
        reader.feed_data(encode(response)+b'\n')
        class Writer:
            def write(self, raw): pass
            async def drain(self): pass
        session = _Session(SimpleNamespace(timeout=5.), reader, Writer(), None, None)
        session.check = lambda proof: None
        decoded = await session.planned(dict(schema_version=1, action='phase_status',
            scope=body.scope.wire(), attempt_id=item.submission.admission.attempt_id))
        self.assertEqual(decoded, root.status(item), 'wire compaction lost exact proof or Admission')
        self.assertEqual(decoded['native']['attachment'], body.native.status()['attachment'])
        self.assertEqual(decoded['native']['producer'], body.native.mailbox.producer_ack.wire())
        self.assertEqual(decoded['native']['committed'], body.native.mailbox.model_committed.wire())
        self.assertFalse(decoded['acknowledged'])
        await self.status_exchange(root, item, ack=True)

    async def test_budget_reserves_maximum_physical_fields_and_escaped_paths(self):
        from types import SimpleNamespace
        from uuid import UUID
        from mop.node.host_backend import Process
        from mop.node.host_native import phase_parse, status_budget
        root, body, item = self.populated(bounded=True, escaped=True)
        status_budget(SimpleNamespace(reference=body.reference, admission=item.submission.admission,
            projection=body.projection, pins=SimpleNamespace(desired_model='m'*128)), root.manifest)
        box = body.native.mailbox
        scope = replace(box.producer_ack.scope, session_id='s'*128)
        box.producer_ack = ProducerAck(scope, 1)
        box.model_committed = replace(box.model_committed, scope=scope, turn_id=UUID(int=1))
        physical = body.native.physical
        physical['session_id'] = scope.session_id
        physical['runner'] = Process(2**31-1, 2**64-1, body.reference.boot_id).wire()
        for name in ('native', 'tmux'):
            physical[name]['process'] = physical['runner']
        item.pending = SimpleNamespace(effect='producer-attach', sequence=3, approved=False)
        response, size = await self.status_exchange(root, item)
        self.assertEqual(phase_parse(response['phase']), root.status(item))
        response, ack_size = await self.status_exchange(root, item, ack=True)
        self.assertEqual(ack_size, size-1)
        await self.status_exchange(root, item, ack=True)
        self.assertEqual(body.completed, ['native_exec'])

    async def test_inconsistent_proof_is_refused_not_silently_rebound(self):
        from mop.node.host_native import phase_wire, phase_parse
        root, body, item = self.populated()
        phase = root.status(item)
        phase['native']['attachment']['reference'] = {**phase['reference'], 'boot_id': str(uuid4())}
        with self.assertRaisesRegex(Refused, 'reference'):
            phase_wire(phase)
        encoded = phase_wire(root.status(item))
        encoded['native']['committed']['scope'] = body.native.mailbox.producer_ack.scope.wire()
        with self.assertRaisesRegex(Refused, 'scope'):
            phase_parse(encoded)

    async def test_unsupported_escaped_installation_rejects_before_native_dispatch(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from host_native import model_delivery
        from mop.node.host_inputs import pack
        from mop.node.host_native import NativeSubmission, ModelInputs, ProviderProfile
        from mop.node.host_native_owner import submit_native
        from mop.node.host_phases import Projection
        from mop.node.host_scope import Reference
        a, scope, delivery = model_delivery()
        profile = ProviderProfile('owned-fixture-v1', 'https://provider.invalid', Path('/opt/ca'), 'api-key-v1')
        projection = Projection.from_delivery(delivery)
        reference = Reference(scope, uuid4().hex, str(uuid4()))
        request = NativeSubmission(a, pack(a, scope, delivery).descriptor, projection,
            reference, ModelInputs.parse(delivery, profile).pins())
        root, _, _ = self.populated()
        root.bodies = {scope: SimpleNamespace(reference=reference, native=None, projection=projection,
            completed=['prepare_body', 'bootstrap_body', 'initial_identity'], attempts=[], cleanup=None)}
        root._closed, root._confirmed, root._bound_epoch, root._backend_epoch = False, True, scope.epoch, scope.executor_epoch
        root.identity = SimpleNamespace(node_id=scope.node_id)
        root.installed = SimpleNamespace(runner=Path('/fixed/release/mop/node/host_body.py'))
        root._owned, root.attempts, root.capacity = {scope: reference}, {}, 256
        root.manifest.tools = dict(native=Path('/opt/'+'\x01'*235), tmux=Path('/opt/'+'\x02'*235))
        root.manifest.require_native_release = Mock(side_effect=AssertionError('budget check was too late'))
        root._store = Mock(side_effect=AssertionError('unsupported combination reached snapshot/dispatch'))
        with self.assertRaisesRegex(Refused, 'status.*budget|frame.*budget'):
            submit_native(root, request)
        root._store.assert_not_called()
        self.assertIsNone(root.bodies[scope].native)
        self.assertEqual(root.attempts, {})


# BUG #782: полученная poll-записью операция не является текущим правом на HTTP
# после close/replace/TTL.
# HYPOTHESIS: Root проверяется только при poll/complete, не на dispatch и во время HTTP.
# RESULT (RED на ab834133): закрытый mailbox, отменённый request и исчезнувший
# native всё равно дают один HTTP вызов; poll не несёт 5s deadline; старый
# frontend забирает ticket замены; начатый dispatch снова выдаётся замене;
# HTTP не отменяется ни Root, ни остатком бюджета; Unknown Root exchange убивает worker.
# SOLUTION: poll только доставка. native_dispatch (begin) и native_validate —
# свежая Root readback с exact frontend/ticket/digest/original deadline перед,
# во время (watcher) и после HTTP, плюс перед первыми байтами после TLS. HTTP
# — задача dispatch owner под тем же deadline; начатый dispatch только unknown.
# Telemetry по-прежнему без Work.gate: phase lease не право lifetime.
# STATUS: FIXED — see #782
class ProxyDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from types import SimpleNamespace
        from uuid import UUID
        from host_native import model_delivery
        from mop.common.producer import ProducerReport
        from mop.common.telemetry import SessionScope
        from mop.node.host_scope import Reference
        from mop.node.host_native_client import LifetimeWorker
        a, scope, delivery = model_delivery()
        self.scope, self.admission = scope, a
        self.reference = Reference(scope, uuid4().hex, str(uuid4()))
        self.registration = Registration(a.work, delivery['bootstrap_revision'], delivery['bootstrap_digest'],
            delivery['image_digest'], delivery['display_name'])
        session = SessionScope(*(UUID(getattr(a.work, k)) for k in
            ('epoch', 'target_id', 'incarnation_id', 'node_id', 'executor_epoch')), str(uuid4()), uuid4())
        report = ProducerReport(session, UUID(self.registration.bootstrap_revision), self.registration.bootstrap_digest,
            self.registration.image_digest, self.registration.display_name, '%1', 12345,
            self.reference.boot_id+':12345678')
        identity = dict(pid=report.pid, process_start=report.process_start, session_id=session.session_id,
            tmux_pid=12346, tmux_start=12345679, socket_inode=[123, 456], tty_identity=[136, 1, 123],
            namespaces=['mnt:[4026531841]', 'pid:[4026531836]', 'user:[4026531837]'])
        self.box = api().NativeMailbox(self.registration, a, desired_model='fixture-desired')
        registration = asyncio.create_task(self.box.request('register_producer', dict(report=report.wire(), identity=identity)))
        await asyncio.sleep(0)
        record = self.box.poll('seed')
        self.box.complete('seed', record['id'], record['ticket'], ProducerAck(session, 1).wire())
        await registration
        self.row = Observation(session, 1, 'PostModelSwitch', None, 'fixture-desired', None, None, 0)
        self.calls, self.exchanges, self.requests = [], [], []
        self.client = MachineClient(object.__new__(HttpsTransport))
        self.worker = LifetimeWorker(self.client, None, scope, a.attempt_id)
        self.alive = True
        self.checks = 0
        class Life:
            async def check(life):
                self.checks += 1
                if not self.alive:
                    raise Refused('original native attachment is gone')
        self.life = Life()
        self.life.mailbox = self.box
        item = SimpleNamespace()
        self.life.item = item
        self.root = SimpleNamespace(_attempt=lambda *args: item,
            bodies={scope: SimpleNamespace(native=self.life, cleanup=None)})
        async def exchange(value):
            from mop.node.host_native_owner import mailbox_exchange
            self.exchanges.append(value['action'])
            try:
                result = await mailbox_exchange(self.root, value, 'node-process')
            except (Refused, api().Unavailable):
                self.worker.closed = True
                raise
            if value['action'] == 'native_complete':
                self.worker.closed = True
            return result
        self.worker.exchange = exchange
        async def http(transport, *args, **kwargs):
            return await self.http(transport, *args, **kwargs)
        self.transport_request = http

    async def asyncTearDown(self):
        await self.worker.aclose()
        self.box.close()
        for request in self.requests:
            request.cancel()
        await asyncio.gather(*self.requests, return_exceptions=True)

    async def polled(self, worker=None):
        request = asyncio.create_task(self.box.request('ingest_telemetry', self.row.wire()))
        self.requests.append(request)
        await asyncio.sleep(0)
        if worker is not None:
            return request, self.box.poll(worker)
        # Тот же Root exchange, что у actual worker: identity записи равна его
        # dispatch identity, отказ ниже не маскируется чужим ticket.
        value = await self.worker.exchange(self.worker.address('native_poll'))
        return request, value['operation']

    async def http(self, transport, method, path, payload=None, params=None):
        self.calls.append((method, path, payload))
        return Ack(self.row.scope.producer_epoch, self.row.sequence).wire()

    async def test_closed_polled_record_cannot_call_execute_proxy_transport(self):
        request, record = await self.polled()
        self.box.close()
        await asyncio.gather(request, return_exceptions=True)
        with patch.object(HttpsTransport, 'request', self.transport_request):
            try:
                await api().execute_proxy(self.client, record)
            except (Refused, api().Unavailable):
                pass
        self.assertEqual(len(self.calls), 0, 'closed mailbox still dispatched HTTP before completion refusal')

    async def test_actual_worker_revalidates_late_poll_before_http(self):
        request, record = await self.polled()
        self.box.close()
        await asyncio.gather(request, return_exceptions=True)
        original_exchange = self.worker.exchange
        async def exchange(value):
            if value['action'] == 'native_poll':
                return dict(operation=record)
            self.worker.closed = True
            return await original_exchange(value)
        self.worker.exchange = exchange
        with patch.object(HttpsTransport, 'request', self.transport_request):
            async with asyncio.timeout(1):
                await self.worker.run()
        self.assertEqual(len(self.calls), 0, 'actual LifetimeWorker dispatched a closed polled record')

    async def test_original_deadline_is_carried_without_renewal(self):
        request, record = await self.polled()
        item = self.box.operations[record['id']]
        self.assertIn('deadline', record, 'poll drops the original five-second operation cutoff')
        self.assertEqual(record['deadline'], item.deadline)
        replacement = self.box.poll('replacement')
        self.assertEqual(replacement['deadline'], item.deadline)
        self.assertNotEqual(replacement['ticket'], record['ticket'])
        request.cancel()

    # BUG #782: expired original operation не начинает HTTP, даже если poll
    # отдал запись до истечения, а HTTP ждал budget.
    # HYPOTHESIS: dispatch смотрел только cached record и не сверял deadline.
    # SOLUTION: NativeDispatch.local()/check() сверяют тот же monotonic deadline
    # до, вокруг и после HTTP; asyncio.timeout_at бюджета — не новый 5s.
    # STATUS: FIXED — see #782
    async def test_expired_operation_never_starts_http(self):
        import time
        request, record = await self.polled()
        # Запись уже доставлена, но Root lifetime истёк до dispatch.
        self.box.operations[record['id']].deadline = time.monotonic()-1
        original = self.worker.exchange
        async def exchange(value):
            if value['action'] == 'native_poll':
                return dict(operation=record)
            return await original(value)
        self.worker.exchange = exchange
        with patch.object(HttpsTransport, 'request', self.transport_request):
            async with asyncio.timeout(1):
                await self.worker.run()
        self.assertEqual(self.calls, [], 'expired operation still reached HTTP transport')
        self.assertIsNone(self.box.model_committed)
        request.cancel()

    async def test_stale_frontend_ticket_never_starts_http(self):
        request, record = await self.polled()
        frontend = self.worker.frontend
        # Замена frontend выдала новый ticket; старая запись уже не право на HTTP.
        replacement = self.box.poll('replacement-'+frontend)
        self.assertNotEqual(record['ticket'], replacement['ticket'])
        original = self.worker.exchange
        async def exchange(value):
            if value['action'] == 'native_poll':
                return dict(operation=record)
            return await original(value)
        self.worker.exchange = exchange
        with patch.object(HttpsTransport, 'request', self.transport_request):
            async with asyncio.timeout(1):
                await self.worker.run()
        self.assertEqual(self.calls, [], 'stale frontend ticket still reached HTTP')
        self.assertIsNone(self.box.model_committed)
        self.box.complete('replacement-'+frontend, replacement['id'], replacement['ticket'],
            Ack(self.row.scope.producer_epoch, self.row.sequence).wire())
        await request

    async def queued(self):
        request = asyncio.create_task(self.box.request('ingest_telemetry', self.row.wire()))
        self.requests.append(request)
        await asyncio.sleep(0)
        return request

    async def run_worker(self):
        with patch.object(HttpsTransport, 'request', self.transport_request):
            async with asyncio.timeout(2):
                await self.worker.run()

    async def test_alive_worker_telemetry_remains_legal_after_phase_expiry(self):
        request = await self.queued()
        future = self.admission.work.not_after+timedelta(seconds=1)
        class Expired(datetime):
            @classmethod
            def now(cls, tz=None): return future
        with patch('mop.node.http_execution.datetime', Expired):
            await self.run_worker()
        await request
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.box.model_committed, self.row)
        self.assertGreaterEqual(self.checks, 3, 'no fresh Root validation around HTTP')
        self.assertIn('native_dispatch', self.exchanges)
        self.assertIn('native_validate', self.exchanges)
        # Duplicate completion не создаёт второй COMMIT/dispatch и не меняет proof.
        self.assertEqual(self.box.last_ack, Ack(self.row.scope.producer_epoch, 1))

    async def test_cancelled_original_request_prevents_late_polled_dispatch(self):
        request, record = await self.polled()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        exchange = self.worker.exchange
        async def delayed(value):
            if value['action'] == 'native_poll': return dict(operation=record)
            return await exchange(value)
        self.worker.exchange = delayed
        await self.run_worker()
        self.assertEqual(len(self.calls), 0)

    async def test_disappeared_native_before_dispatch_has_no_http(self):
        request, record = await self.polled()
        exchange = self.worker.exchange
        async def delayed(value):
            if value['action'] == 'native_poll':
                self.alive = False
                return dict(operation=record)
            return await exchange(value)
        self.worker.exchange = delayed
        await self.run_worker()
        self.assertEqual(len(self.calls), 0)
        self.assertIsNone(self.box.model_committed)
        request.cancel()

    async def test_inflight_native_disappearance_cancels_http_without_new_hook_traffic(self):
        request = await self.queued()
        cancelled = asyncio.Event()
        async def blocked(transport, method, path, payload=None, params=None):
            self.calls.append((method, path, payload))
            self.alive = False
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.http = blocked
        await self.run_worker()
        self.assertTrue(cancelled.is_set(), 'HTTP task escaped its lifetime owner')
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.box.model_committed)
        self.assertIn('unknown', self.worker.last_error)
        request.cancel()

    async def test_remaining_operation_budget_cancels_http_not_a_new_five_seconds(self):
        import time
        request = await self.queued()
        operation = next(iter(self.box.operations.values()))
        operation.deadline = time.monotonic()+.05
        cancelled = asyncio.Event()
        async def blocked(transport, method, path, payload=None, params=None):
            self.calls.append((method, path, payload))
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.http = blocked
        before = time.monotonic()
        await self.run_worker()
        self.assertLess(time.monotonic()-before, .5)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.box.model_committed)
        request.cancel()

    async def test_lost_root_readback_cancels_http_as_unknown_and_worker_survives(self):
        # BackendClient превращает OSError/timeout Root exchange в Unknown: это
        # не право продолжать HTTP и не причина терять frontend worker.
        from mop.node.host_scope import Unknown
        request = await self.queued()
        exchange = self.worker.exchange
        polls = 0
        async def unreachable(value):
            nonlocal polls
            if value['action'] == 'native_poll':
                polls += 1
                if polls == 1:
                    raise Unknown('HOST backend authority connection unavailable; no automatic retry')
            if value['action'] == 'native_validate' and self.calls:
                raise Unknown('HOST backend authority connection unavailable; no automatic retry')
            return await exchange(value)
        self.worker.exchange = unreachable
        cancelled = asyncio.Event()
        async def blocked(transport, method, path, payload=None, params=None):
            self.calls.append((method, path, payload))
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.http = blocked
        await self.run_worker()
        self.assertTrue(cancelled.is_set(), 'HTTP kept running after its Root readback was lost')
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.box.model_committed)
        results = await asyncio.gather(request, return_exceptions=True)
        self.assertIsInstance(results[0], api().Unavailable)
        self.assertIn('unknown', str(results[0]))

    async def actual_https(self, *, close_on_connect=False):
        import aiohttp
        import http.server
        import ssl
        from _control_fixture import own_tls
        test = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.server.calls.append(self.path)
                row = Observation.parse(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                raw = json.dumps(Ack(row.scope.producer_epoch, row.sequence).wire()).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        request = await self.queued()
        original = aiohttp.TCPConnector.connect
        async def connect(connector, *args, **kwargs):
            connection = await original(connector, *args, **kwargs)
            if close_on_connect:
                test.box.close()
            return connection
        with own_tls(Handler) as (server, origin, cert, _):
            server.calls = []
            transport = HttpsTransport(origin, 'owned-disposable-node-token',
                tls=ssl.create_default_context(cafile=str(cert)))
            self.client = self.worker.client = MachineClient(transport)
            try:
                with patch.object(aiohttp.TCPConnector, 'connect', connect):
                    async with asyncio.timeout(2): await self.worker.run()
                return list(server.calls), await asyncio.gather(request, return_exceptions=True)
            finally:
                await transport.aclose()

    async def test_root_closed_during_tls_connect_prevents_actual_http_write(self):
        calls, result = await self.actual_https(close_on_connect=True)
        self.assertEqual(calls, [], 'TLS wait used cached Root permission to send new HTTP after close')
        self.assertIsInstance(result[0], api().Unavailable)
        self.assertIsNone(self.box.model_committed)

    async def test_alive_actual_verified_tls_dispatch_still_commits_exact_row(self):
        calls, result = await self.actual_https()
        self.assertEqual(calls, ['/api/v1/machine/telemetry'])
        self.assertEqual(result, [Ack(self.row.scope.producer_epoch, 1).wire()])
        self.assertEqual(self.box.model_committed, self.row)

    async def test_frontend_shutdown_owns_and_drains_pending_http(self):
        await self.queued()
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def blocked(transport, method, path, payload=None, params=None):
            self.calls.append((method, path, payload))
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.http = blocked
        with patch.object(HttpsTransport, 'request', self.transport_request):
            self.worker.start()
            async with asyncio.timeout(1): await started.wait()
            await self.worker.aclose()
        self.assertTrue(cancelled.is_set())
        self.assertTrue(self.worker.task.done())
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.box.model_committed)

    async def test_replacement_after_dispatch_is_unknown_never_reissued(self):
        request = await self.queued()
        cancelled = asyncio.Event()
        async def blocked(transport, method, path, payload=None, params=None):
            self.calls.append((method, path, payload))
            replacement = self.box.poll('replacement')
            self.assertIsNone(replacement, 'already dispatched operation was offered for replay')
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.http = blocked
        await self.run_worker()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.box.model_committed)
        results = await asyncio.gather(request, return_exceptions=True)
        self.assertIsInstance(results[0], api().Unavailable)
        self.assertIn('unknown', str(results[0]))

    async def test_replaced_frontend_cannot_reclaim_current_ticket(self):
        request, record = await self.polled('old')
        replacement = self.box.poll('replacement')
        with self.assertRaisesRegex(Refused, 'retired|replaced|frontend'):
            self.box.poll('old')
        self.box.complete('replacement', replacement['id'], replacement['ticket'],
            Ack(self.row.scope.producer_epoch, self.row.sequence).wire())
        await request
        self.assertEqual(self.box.model_committed, self.row)


if __name__ == '__main__':
    unittest.main()
