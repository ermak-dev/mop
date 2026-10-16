"""Реальный stdlib subprocess/Unix IPC #641, без стороннего CLI и установки.

HYPOTHESIS: hook пишет локальный JSON и теряет scope/границы нагрузки.
RED: старый hook создаёт .local и не соединяется с Unix bridge; затем
воспроизведены зависание stdin, fresh replay старой модели и attach/close гонки.
SOLUTION: bounded Unix bridge с ephemeral credential, без state-файлов.
RESULT: 14/14, включая реальный stdlib subprocess, Unix socket и verified HTTPS.
STATUS: FIXED — see #641; Root composition остаётся отдельной приёмкой.
"""
import _lib
import hermetic
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mop import session


class Values(unittest.TestCase):
    def test_initiator_session_is_endpoint_session_identity_not_external_cli_name(self):
        from mop.common.telemetry import SourceRef
        # #649 TrustedEndpointContext.session_id — UUID, external CLI id отдельно.
        value = SourceRef(1, uuid4(), uuid4(), uuid4(), uuid4(), uuid4())
        self.assertIsNotNone(value.session_id)
        with self.assertRaises(ValueError):
            SourceRef(1, uuid4(), uuid4(), 'external-cli-name', uuid4(), uuid4())


class Hook(unittest.TestCase):
    def test_standalone_hook_emits_bounded_record_and_creates_no_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory+'/hook.sock'
            token = 'c'*64
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(path)
                server.listen(1)
                server.settimeout(.5)
                env = dict(os.environ, HOME=directory, MOP_TELEMETRY_SOCKET=path, MOP_TELEMETRY_TOKEN=token)
                proc = subprocess.Popen([sys.executable, '-I', str(ROOT/'mop/session.py'), 'hook'],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                out, err = proc.communicate(json.dumps(dict(session_id='sid', hook_event_name='StopFailure',
                    error='model_not_found', last_assistant_message='never forward private text')).encode(), timeout=2)
                self.assertEqual((proc.returncode, out, err), (0, b'', b''))
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    self.fail('stdlib hook must emit a bounded Unix IPC record, not a turn/model file')
                with conn:
                    body = json.loads(conn.recv(4097))
                self.assertEqual(body['token'], token)
                self.assertEqual(body['record'], dict(schema_version=1, session_id='sid', event='StopFailure',
                    model=None, error='model_not_found'))
                self.assertEqual(sorted(os.listdir(directory)), ['hook.sock'])

    def test_hook_missing_or_stalled_bridge_is_quiet_bounded_and_never_creates_files(self):
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home, MOP_TELEMETRY_SOCKET=home+'/absent', MOP_TELEMETRY_TOKEN='a'*64)
            start = time.monotonic()
            for verb in ('hook', 'statusline'):
                result = subprocess.run([sys.executable, '-I', str(ROOT/'mop/session.py'), verb],
                    input=json.dumps(dict(session_id='sid', hook_event_name='Stop', model={'id':'model-a'})),
                    env=env, capture_output=True, text=True, timeout=2)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
            self.assertLess(time.monotonic()-start, 1.5)
            self.assertEqual(os.listdir(home), [])

    def test_switch_requires_actual_switch_payload_not_a_passive_model_field(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory+'/ipc.sock'
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(path)
                server.listen(1)
                server.settimeout(.1)
                env = dict(os.environ, HOME=directory, MOP_TELEMETRY_SOCKET=path, MOP_TELEMETRY_TOKEN='a'*64)
                subprocess.run([sys.executable, '-I', str(ROOT/'mop/session.py'), 'hook'],
                    input=json.dumps(dict(session_id='sid', hook_event_name='PostModelSwitch', model={'id':'passive'})),
                    env=env, capture_output=True, text=True, timeout=2, check=True)
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    pass
                else:
                    conn.close()
                    self.fail('model.id without to_model cannot confirm a model switch')

    def test_stalled_stdin_cannot_hold_the_cli_hook_open(self):
        with tempfile.TemporaryDirectory() as home:
            proc = subprocess.Popen([sys.executable, '-I', str(ROOT/'mop/session.py'), 'hook'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ, HOME=home))
            try:
                proc.stdin.write(b'{')
                proc.stdin.flush()
                try:
                    proc.wait(timeout=.75)
                except subprocess.TimeoutExpired:
                    self.fail('hook stdin must have an absolute time budget, not wait for EOF forever')
                self.assertEqual(proc.returncode, 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate()
            self.assertEqual(os.listdir(home), [])

    def test_no_owned_json_reader_writer_or_mop_import_remains(self):
        import ast
        tree = ast.parse((ROOT/'mop/session.py').read_text())
        names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        self.assertFalse(names & {'_write_turn', 'write_model', 'read_model', 'read_turn'})
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or '').startswith(('mop', 'django')))
        self.assertTrue(callable(session.envelope))
        self.assertTrue(callable(session.history))


class Bridge(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('mop.node.telemetry'), 'async telemetry bridge must exist')
        from mop.node.telemetry import HookBridge
        from mop.common.telemetry import SessionScope
        self.scope = SessionScope(*(uuid4() for _ in range(5)), 'sid', uuid4())
        test = self
        class Client:
            fail = False
            def __init__(inner):
                inner.rows = []
            async def ingest(inner, row):
                if inner.fail:
                    raise OSError('test offline')
                inner.rows.append(row)
                from mop.common.telemetry import Ack
                return Ack(row.scope.producer_epoch, row.sequence)
        self.client = Client()
        self.directory = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(asyncio.to_thread, self.directory.cleanup)
        self.bridge = HookBridge(self.client, self.scope, path=self.directory.name+'/ipc.sock',
            token='a'*64, capacity=2, interval=.01)
        self.addAsyncCleanup(self.bridge.aclose)

    def record(self, **values):
        return dict(schema_version=1, session_id='sid', event='Stop', model=None, error=None, **values)

    async def test_real_unix_hook_reaches_async_bridge_http_client_and_shutdown_is_owned(self):
        await self.bridge.start()
        env = dict(os.environ, HOME=self.directory.name, **self.bridge.environment())
        process = await asyncio.create_subprocess_exec(sys.executable, '-I', str(ROOT/'mop/session.py'), 'hook',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        self.assertEqual(await process.communicate(json.dumps(dict(session_id='sid', hook_event_name='Stop')).encode()), (b'', b''))
        for _ in range(100):
            if self.client.rows:
                break
            await asyncio.sleep(.01)
        self.assertEqual(len(self.client.rows), 1)
        self.assertEqual(self.client.rows[0].scope, self.scope)
        self.assertIsNone(self.client.rows[0].causal_switch, 'arrival is not model observation causality')
        await self.bridge.aclose()
        self.assertFalse(self.bridge.pending_tasks)
        self.assertFalse(os.path.exists(self.bridge.path))
        self.assertEqual(os.listdir(self.directory.name), [])

    async def test_outage_overflow_gap_reconnect_and_wrong_session_token_are_bounded(self):
        self.client.fail = True
        for _ in range(5):
            self.bridge.accept(self.record(), token='a'*64)
        self.assertEqual(self.bridge.buffered, 2)
        self.assertEqual(self.bridge.dropped, 3)
        self.assertFalse(self.bridge.accept(self.record(), token='wrong'))
        self.assertFalse(self.bridge.accept(dict(self.record(), session_id='foreign'), token='a'*64))
        await self.bridge.flush()
        self.assertEqual(self.bridge.buffered, 2)
        self.client.fail = False
        await self.bridge.flush()
        self.bridge.accept(self.record(), token='a'*64)
        await self.bridge.flush()
        self.assertEqual([r.sequence for r in self.client.rows], [1, 2, 6])
        self.assertEqual(self.client.rows[-1].dropped, 3)

    async def test_trusted_processed_turn_correlation_is_not_overridden_by_hook_identity(self):
        method = getattr(self.bridge, 'bind_processed_turn', None)
        self.assertTrue(callable(method), 'delivery adapter needs an explicit actual-turn correlation port')
        turn_id = uuid4()
        method(turn_id)
        self.bridge.accept(dict(self.record(), event='UserPromptSubmit'), token='a'*64)
        self.bridge.accept(self.record(), token='a'*64)
        await self.bridge.flush()
        self.assertEqual([row.turn_id for row in self.client.rows], [turn_id, turn_id])
        self.assertFalse(self.bridge.accept(dict(self.record(), owner_id='forged'), token='a'*64))

    async def test_runtime_shutdown_cannot_leave_an_inflight_attach_or_socket(self):
        await self.check_close_during_attach(shutdown=True)

    async def test_runtime_detach_cannot_leave_an_inflight_attach_or_socket(self):
        await self.check_close_during_attach(shutdown=False)

    async def check_close_during_attach(self, *, shutdown):
        from unittest.mock import patch
        from mop.node.telemetry import TelemetryRuntime
        runtime = TelemetryRuntime(object(), limit=1)
        self.addAsyncCleanup(runtime.aclose)
        entered, release = asyncio.Event(), asyncio.Event()
        original = asyncio.start_unix_server
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        path = self.directory.name+'/owned.sock'
        with patch('asyncio.start_unix_server', side_effect=delayed):
            task = asyncio.create_task(runtime.attach(self.scope, path=path, token='a'*64))
            await asyncio.wait_for(entered.wait(), 1)
            if shutdown:
                await runtime.aclose()
            else:
                await runtime.detach(self.scope.target_id)
            release.set()
            results = await asyncio.gather(task, return_exceptions=True)
        try:
            self.assertIsInstance(results[0], BaseException, 'closed runtime cannot return a new body environment')
            self.assertFalse(os.path.exists(path), 'shutdown owns in-flight startup as well as worker tasks')
        finally:
            # Даже заведомо красный тест не оставляет созданную им CLI-сессию/сокет.
            for bridge in tuple(runtime._bridges.values()):
                await bridge.aclose()

    async def test_expired_offline_buffer_is_a_gap_not_fresh_model_evidence(self):
        self.bridge.age_limit = .01
        self.bridge.accept(dict(self.record(), event='PostModelSwitch', model='old-model'), token='a'*64)
        await asyncio.sleep(.03)
        await self.bridge.flush()
        self.assertEqual(self.client.rows, [], 'an old buffered switch must not acquire fresh receive time')
        self.assertEqual(self.bridge.dropped, 1)
        self.bridge.accept(self.record(), token='a'*64)
        await self.bridge.flush()
        self.assertEqual(self.client.rows[0].sequence, 2)
        self.assertEqual(self.client.rows[0].dropped, 1)

    async def test_real_verified_https_carries_only_machine_scope_and_commit_ack(self):
        import ssl
        from mop.node.http_execution import HttpsTransport
        from mop.node.telemetry import TelemetryClient
        from mop.common.telemetry import Observation
        directory = Path(self.directory.name)
        cert, key = directory/'test.pem', directory/'test.key'
        command = ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
            '-keyout', str(key), '-out', str(cert)]
        result = await asyncio.to_thread(subprocess.run, command, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_tls.load_cert_chain(cert, key)
        client_tls = ssl.create_default_context(cafile=str(cert))
        seen = []
        async def receive(reader, writer):
            try:
                async with asyncio.timeout(2):
                    head = (await reader.readuntil(b'\r\n\r\n')).decode()
                    length = int(next(line.split(':', 1)[1] for line in head.splitlines()
                                      if line.lower().startswith('content-length:')))
                    body = json.loads(await reader.readexactly(length))
                seen.append((head, body))
                data = json.dumps(dict(schema_version=1, producer_epoch=body['scope']['producer_epoch'],
                                       sequence=body['sequence'])).encode()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: '+
                             str(len(data)).encode()+b'\r\n\r\n'+data)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(receive, '127.0.0.1', 0, ssl=server_tls)
        transport = HttpsTransport('https://127.0.0.1:'+str(server.sockets[0].getsockname()[1]),
                                   'fixture-machine-token', tls=client_tls)
        try:
            client = TelemetryClient(transport)
            row = Observation(self.scope, 1, 'Stop', uuid4(), None, None, None, 0)
            self.assertEqual((await client.ingest(row)).sequence, 1)
            self.assertTrue(seen[0][0].startswith('POST /api/v1/machine/telemetry HTTP/1.1'))
            self.assertIn('Authorization: Token fixture-machine-token', seen[0][0])
            self.assertEqual(seen[0][1], row.wire())
        finally:
            await transport.aclose()
            server.close()
            await server.wait_closed()

    async def test_http_client_uses_named_authenticated_transport_and_validates_ack(self):
        from mop.node.telemetry import TelemetryClient
        from mop.common.telemetry import Observation
        class Transport:
            async def request(inner, method, path, payload=None, params=None):
                self.assertEqual((method, path), ('POST', 'telemetry'))
                self.assertEqual(payload['scope']['node_id'], str(self.scope.node_id))
                return dict(schema_version=1, producer_epoch=str(self.scope.producer_epoch), sequence='1')
        client = TelemetryClient(Transport())
        row = Observation(self.scope, 1, 'Stop', uuid4(), None, None, None, 0)
        self.assertEqual((await client.ingest(row)).sequence, 1)


if __name__ == '__main__':
    unittest.main()
