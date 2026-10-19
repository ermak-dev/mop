"""Клиент #773: настоящий TLS/WSS и Unix peer, не native application proof.

HYPOTHESIS: конечный report intent и launcher-owned receiver отсутствуют.
SPEC: ключ до I/O, unknown только GET, native guard/evidence без replay.
SOLUTION: typed report intent, одноразовый verified WSS owner и общий MCP port.
RESULT: функциональный RED (missing intent/receiver/port; close race; holder fence)
сменился green на настоящих TLS/WSS и owned Unix peer. Отдельный PG17 slice
проверяет lost committed POST → GET без repost; native 2.1.302 fixture bind
не дал matched acceptance/idle, его outcome остаётся unknown, не green proof.
STATUS: FIXED — see #773 (client invariants; native/production availability open).
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import _lib  # noqa: F401
import hermetic  # noqa: F401
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mop.client import control as api
from mop.common.endpoint import inbox_encode, inbox_decode
from _control_fixture import Fixture, Response
from _own_session_fixture import own_session


def key():
    return str(uuid4())


# Зелёный `mop dev test` молчит: opt-in файл сохраняет те же resource
# snapshots как evidence зелёного прогона. На assertions не влияет, имя без
# MOP_ — hermetic его не стирает.
EVIDENCE = os.environ.get('PRIVATE_INBOX_EVIDENCE')


def evidence(label, value):
    line = label+' '+json.dumps(value)
    print(line, flush=True)
    if EVIDENCE:
        with open(EVIDENCE, 'a') as out:
            out.write(line+'\n')


class Reports(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.patch = patch.object(api, '_DeadlineHTTPSConnection', self.fixture.factory)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = api.Client(api.ServerContext('https://localhost', 'fixture-private-token'))
        self.epoch, self.message, self.operation, self.delivery = key(), key(), key(), key()

    def intent(self, **changes):
        self.assertTrue(callable(getattr(self.client, 'prepare_report', None)),
            'finite private report intent is missing')
        return self.client.prepare_report(**dict(dict(address='private:'+key(), epoch=self.epoch,
            message_id=self.message, operation_request_id=self.operation, text='private-marker',
            not_after=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()), **changes))

    def result(self, **changes):
        return dict(schema_version=1, delivery=dict(dict(id=self.delivery, message_id=self.message,
            operation_request_id=self.operation, state='queued', idle=False), **changes))

    def test_report_fixed_message_identity_pending_not_application_acceptance(self):
        intent = self.intent()
        self.assertNotIn('private-marker', repr(intent))
        self.fixture.responses.append(Response(202, self.result()))
        result = self.client.report(intent)
        self.assertEqual(result.state, 'queued')
        method, path, body, headers = self.fixture.calls[0]
        self.assertEqual((method, path), ('POST', '/api/v1/reports'))
        self.assertEqual(json.loads(body)['message_id'], self.message)
        self.assertEqual(json.loads(body)['operation_request_id'], self.operation)
        self.assertNotIn('X-Mop-Connection', headers)

    def test_lost_post_recover_exact_sender_key_404_still_unknown_no_repost(self):
        intent = self.intent()
        self.fixture.responses.append(OSError('private-marker'))
        with self.assertRaises(api.ClientError) as caught:
            self.client.report(intent)
        self.assertEqual(getattr(caught.exception, 'message_id', None), self.message)
        self.assertNotIn('private-marker', repr(caught.exception))
        self.fixture.responses.append(Response(404, dict(schema_version=1, epoch=self.epoch,
            error=dict(code='not_found', message='delivery not found', retryable=False))))
        self.assertIsNone(self.client.recover_report(intent))
        self.fixture.responses.append(Response(200, self.result()))
        self.assertEqual(self.client.recover_report(intent).id, uuid4().__class__(self.delivery))
        self.assertEqual([x[0] for x in self.fixture.calls], ['POST', 'GET', 'GET'])
        self.assertEqual(self.fixture.calls[1][1],
            '/api/v1/deliveries?schema_version=1&epoch='+self.epoch+'&message_id='+self.message)

    def test_private_text_bytes_do_not_raise_machine_send_limit(self):
        self.intent(text='\x01'*65536)
        for text in ('é'*32769, 'x\x00', '', '\ud800'):
            with self.assertRaises(api.ClientError):
                self.intent(text=text)
        with self.assertRaises(api.ClientError):
            api.message_text('x'*16385)
        self.assertEqual(api.MAX_MESSAGE_BYTES, 16384)

    def test_retirement_empty_response_still_surfaces_close_failure(self):
        response = Response(204, raw=b'', headers={'Content-Type': ''})
        self.fixture.responses.append(response)
        def fail_close():
            response.closed = True
            raise OSError('fixture-private-token')
        with patch.object(response, 'close', fail_close):
            with self.assertRaises(api.TransportError) as caught:
                self.client._exchange('DELETE', '/api/v1/endpoints/'+key())
        self.assertNotIn('fixture-private-token', str(caught.exception))
        self.assertTrue(self.fixture.connections[-1].closed)

    def test_known_local_holder_refusal_is_not_an_unknown_post(self):
        with self.assertRaises(api.ClientError) as caught:
            self.client.report(self.intent(), holder=object())
        self.assertNotIsInstance(caught.exception, api.UnknownReport)
        self.assertFalse(self.fixture.connections, 'local proof must fail before TLS I/O')

    def test_mismatched_or_malformed_success_is_unknown_never_new_post(self):
        for changes in (dict(message_id=key()), dict(operation_request_id=key()),
                        dict(state='delivered'), dict(state='queued', idle=True)):
            intent = self.intent()
            self.fixture.responses.append(Response(202, self.result(**changes)))
            with self.assertRaises(api.ClientError) as caught:
                self.client.report(intent)
            self.assertEqual(getattr(caught.exception, 'message_id', None), self.message)
        self.assertTrue(all(x[0] == 'POST' for x in self.fixture.calls))


class NativeBoundary(unittest.TestCase):
    def test_native_fixture_has_explicit_small_source_closure_and_no_host_path(self):
        from mop.client import chatprobe_isolation as boundary
        from mop.client import chatprobe
        self.assertTrue(hasattr(boundary, 'INBOX_SOURCES'), 'owned native inbox source closure is missing')
        selected = boundary.fixture_sources([*boundary.SOURCES, *boundary.INBOX_SOURCES,
            '.env', '.claude/settings.json', 'inventory.yaml'], private_inbox=True)
        self.assertEqual(set(selected), set(boundary.SOURCES) | set(boundary.INBOX_SOURCES))
        self.assertNotIn('.env', selected)
        with self.assertRaises(ValueError):
            chatprobe.run('/must-not-create', private_inbox=True)


class Commandlets(Reports):
    def test_declarative_report_dispatch_pending_and_recovery_without_post_retry(self):
        from mop.cli import _control, _mcp
        self.assertEqual(_control.SURFACES.get('report'), 'bound', 'report must use existing dispatcher')
        import mop.cli.core.report as command
        self.assertIn('execution.deliver_report', command.MCP['actions'])
        self.assertFalse(command.MCP.get('human_only', False), 'Agent reports use Agent authority')
        self.assertFalse(_mcp.PRIVATE_INBOX['available'])
        self.fixture.responses.extend([OSError('private-marker'), Response(200, self.result())])
        with _control.use(self.client._context):
            text, status = _control.tool_call(['report'], command.MCP, dict(address='private:'+key(),
                text='private-marker', epoch=self.epoch, message_id=self.message,
                operation_request_id=self.operation), with_status=True)
        self.assertEqual(status, 3, text)
        self.assertEqual(json.loads(text)['delivery']['state'], 'queued')
        self.assertNotIn('private-marker', text)
        self.assertEqual([x[0] for x in self.fixture.calls], ['POST', 'GET'])

    def test_failed_unknown_lookup_preserves_original_key_and_unknown_verdict(self):
        from mop.cli import _control
        import mop.cli.core.report as command
        self.fixture.responses.extend([OSError('private-marker'), OSError('private-marker')])
        with _control.use(self.client._context):
            text, status = _control.tool_call(['report'], command.MCP, dict(address='private:'+key(),
                text='private-marker', epoch=self.epoch, message_id=self.message), with_status=True)
        self.assertEqual(status, 3, text)
        self.assertIn(self.message, text)
        self.assertIn(self.epoch, text)
        self.assertNotIn('private-marker', text)
        self.assertEqual([x[0] for x in self.fixture.calls], ['POST', 'GET'])

    def test_explicit_key_recovery_is_a_readonly_commandlet_without_private_text(self):
        from mop.cli import _control
        import mop.cli.core.delivery as command
        self.fixture.responses.append(Response(200, self.result()))
        with _control.use(self.client._context):
            text, status = _control.tool_call(['delivery'], command.MCP,
                dict(message_id=self.message, epoch=self.epoch), with_status=True)
        self.assertEqual(status, 3, text)
        self.assertEqual(json.loads(text)['delivery']['message_id'], self.message)
        self.assertEqual(self.fixture.calls[0][0], 'GET')

    def test_unsupported_report_native_flags_fail_before_stdin(self):
        from mop.cli import _control
        self.assertEqual(_control.SURFACES.get('report'), 'bound')
        import io
        class Forbidden(io.StringIO):
            def read(self, *args):
                raise AssertionError('unsupported flags consumed private stdin')
        with patch('sys.stdin', Forbidden()), patch('sys.stderr', io.StringIO()):
            self.assertEqual(_control.invoke('report',
                ['private:'+key(), '-', '--epoch', self.epoch, '--socket', '/private']), 1)
        self.assertFalse(self.fixture.calls)


class Receiver(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(importlib.util.find_spec('mop.client.private_inbox'),
            'bounded trusted private receiver is missing')
        self.module = importlib.import_module('mop.client.private_inbox')
        import aiohttp.web
        self.web = aiohttp.web
        self.tmp = tempfile.TemporaryDirectory(prefix='m773-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cert, self.pem = self.root/'ca.pem', self.root/'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-keyout', str(self.pem), '-out', str(self.cert), '-subj', '/CN=localhost',
            '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.endpoint, self.epoch, self.project, self.client_id = key(), key(), key(), key()
        self.nonce, self.external = key(), 'owned-conversation'
        self.frames, self.evidence, self.receipts = asyncio.Queue(), asyncio.Queue(), []
        self.current, self.connected = True, 0
        self.actor = dict(user_id=1, kind='human', human_owner_id=1, node_id=None, grant_id=None)
        self.report_headers = []
        self.drop_receipt = False
        self.native_path = str(self.root/'native.sock')
        self.native_mode = 'delivered'
        self.permit = asyncio.Event(); self.permit.set()
        self.wire_seen = []
        self.http_entered = {name: asyncio.Event() for name in ('capabilities', 'registration')}
        self.http_release = {name: asyncio.Event() for name in self.http_entered}
        for event in self.http_release.values(): event.set()
        self.http_calls = dict.fromkeys(self.http_entered, 0)
        self.ws_entered, self.ws_release = asyncio.Event(), asyncio.Event()
        self.bound_entered, self.bound_release = asyncio.Event(), asyncio.Event()
        self.ws_release.set(); self.bound_release.set()
        async def native(reader, writer):
            try:
                line = await reader.readline()
                if line:
                    frame = json.loads(line)
                    await self.frames.put(frame)
                    callback = frame['from'][4:]
                    _, reply = await asyncio.open_unix_connection(callback)
                    fields = (dict(action='peer_message_status', status=self.native_mode) if frame['type'] == 'user'
                        else dict(action='peer_idle_notice', state='idle'))
                    reply.write((json.dumps(dict(msgV=1, type='control', orig_msg_id=frame['msg_id'], **fields))+'\n').encode())
                    await reply.drain(); reply.close(); await reply.wait_closed()
            finally:
                writer.close(); await writer.wait_closed()
        self.native = await asyncio.start_unix_server(native, path=self.native_path)
        os.chmod(self.native_path, 0o600)
        self.own = own_session(self.native_path, self.external)
        async def capabilities(request):
            self.http_calls['capabilities'] += 1
            self.http_entered['capabilities'].set()
            await self.http_release['capabilities'].wait()
            return self.web.json_response(dict(schema_version=1, catalogue_version=1, epoch=self.epoch,
                actor=self.actor,
                scope=dict(project_id=self.project, node_id=None, client_id=self.client_id),
                actions=['catalog.view_project', 'execution.deliver_report', 'execution.register_endpoint', 'execution.view_delivery']))
        async def register(request):
            body = await request.json()
            self.assertEqual(body['external_session_id'], self.external)
            self.http_calls['registration'] += 1
            self.http_entered['registration'].set()
            await self.http_release['registration'].wait()
            return self.web.json_response(dict(schema_version=1, endpoint=dict(id=self.endpoint,
                session_id=key(), address='private:'+self.endpoint, project_id=self.project,
                external_session_id=self.external, display_name='same display',
                expires_at=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat(), state='registered', live=False)), status=201)
        async def report(request):
            self.report_headers.append(dict(request.headers))
            body = await request.json()
            return self.web.json_response(dict(schema_version=1, delivery=dict(id=key(),
                message_id=body['message_id'], operation_request_id=body['operation_request_id'], state='queued', idle=False)), status=202)
        async def ws(request):
            self.assertEqual(request.headers['Authorization'], 'Token fixture-private-token')
            self.ws_entered.set()
            await self.ws_release.wait()
            if request.transport is None or request.transport.is_closing():
                return self.web.Response(status=503)
            socket = self.web.WebSocketResponse(compress=False, max_msg_size=8192)
            await socket.prepare(request)
            self.socket = socket
            self.connected += 1
            try:
                ready = inbox_decode((await socket.receive()).data, direction='client')
                self.assertEqual(ready['type'], 'peer_ready')
                self.bound_entered.set()
                await self.bound_release.wait()
                if socket.closed or request.transport is None or request.transport.is_closing():
                    return socket
                await socket.send_str(inbox_encode(dict(schema_version=1, type='bound', endpoint_id=self.endpoint,
                    external_session_id=self.external, epoch=self.epoch, generation='1', connection_nonce=self.nonce, lease_seconds=15)))
                raw = await socket.receive()
                if raw.type != __import__('aiohttp').WSMsgType.TEXT:
                    return socket
                self.assertEqual(inbox_decode(raw.data, direction='client')['type'], 'bound_received')
                async for raw in socket:
                    if raw.type != __import__('aiohttp').WSMsgType.TEXT: break
                    value = inbox_decode(raw.data, direction='client'); self.wire_seen.append(value)
                    if value['type'] == 'prewrite':
                        await self.permit.wait()
                        await socket.send_str(inbox_encode(dict(value, type='write_allowed')))
                    elif value['type'] == 'application_acceptance':
                        await self.evidence.put(value)
                        if self.drop_receipt:
                            await socket.close()
                            break
                        receipt = dict(id=value['delivery_id'], message_id=value['message_id'],
                            operation_request_id=None, state=value['outcome'], idle=False)
                        self.receipts.append(receipt)
                        await socket.send_str(inbox_encode(dict(schema_version=1, type='receipt',
                            connection_nonce=self.nonce, result=receipt)))
                    elif value['type'] == 'idle_observed':
                        await self.evidence.put(value)
                        receipt = dict(self.receipts[-1], idle=True)
                        await socket.send_str(inbox_encode(dict(schema_version=1, type='receipt',
                            connection_nonce=self.nonce, result=receipt)))
            finally:
                self.connected -= 1
            return socket
        app = self.web.Application()
        app.router.add_get('/api/v1/capabilities', capabilities)
        app.router.add_post('/api/v1/endpoints', register)
        app.router.add_post('/api/v1/reports', report)
        app.router.add_get('/ws/v1/inbox/{endpoint}', ws)
        self.runner = self.web.AppRunner(app, access_log=None); await self.runner.setup()
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(self.cert, self.pem)
        site = self.web.TCPSite(self.runner, '127.0.0.1', 0, ssl_context=tls); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.context = api.ServerContext('https://127.0.0.1:'+str(port), 'fixture-private-token', str(self.cert))
        self.owner = self.module.InboxOwner(self.context, self.own, client_id=self.client_id,
            project_id=self.project, display_name='same display', current=lambda: self.current)

    async def asyncTearDown(self):
        if hasattr(self, 'owner'):
            self.permit.set()
            callback = self.owner.bridge._callback
            await self.owner.aclose()
            self.assertIsNone(self.owner.holder())
            self.assertFalse(self.owner.tasks)
            self.assertTrue(callback is None or not callback.exists())
            self.native.close(); await self.native.wait_closed()
            await self.runner.cleanup()

    async def delivery(self, idle=False, **changes):
        frame = dict(dict(id=key(), message_id=key(), external_session_id=self.external,
            text='private-marker', priority='now', notify_when_idle=idle, reply_address=None), **changes)
        await self.socket.send_str(inbox_encode(dict(schema_version=1, type='delivery', connection_nonce=self.nonce, frame=frame)))
        return frame

    # HYPOTHESIS (#786): startup не принадлежит cleanup; после HTTP await
    # sweep уже прошёл session=None, а mutable future успел стать None.
    # SPEC: реальные TLS registration/startup barriers, а не post-close flags;
    # success требует quiet session/executor/thread/callback/task/holder.
    # SOLUTION: startup — owned task, который cleanup отменяет и собирает;
    # _check() после каждого await startup; локальная ссылка на future и
    # ограниченный join настоящего worker; success — доказанное состояние
    # (_remaining), неожиданный сбой — refusal на каждом waiter; start(),
    # прерванный чужим close, — ClientError, отменённый вызывающий — cancel.
    # RESULT: на 987e3900 эти проверки RED три раза из трёх (None.done,
    # session после close, 1 live worker, повторный close = success; holder,
    # tasks и native writes = 0). Ужесточение exit/proof RED и на первой
    # попытке repair (CancelledError у неотменённого start, closed-success при
    # шаге без эффекта); затем GREEN, snapshots печатаются до repair.
    # STATUS: FIXED — see #786
    async def repair_startup_fixture(self, owner, executor):
        # Только аварийная уборка теста ПОСЛЕ snapshot/assertion: RED не должен
        # оставлять настоящий worker/socket. Эти действия не служат evidence.
        for event in self.http_release.values(): event.set()
        self.ws_release.set(); self.bound_release.set()
        future = owner._http_future
        if future is not None:
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), 5)
            except (Exception, asyncio.CancelledError):
                pass
        if owner._ws is not None:
            await owner._ws.close()
        if owner._session is not None:
            await owner._session.close()
        await owner.bridge.aclose()
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        self.owner = self.module.InboxOwner(self.context, self.own, client_id=self.client_id,
            project_id=self.project, display_name='same display', current=lambda: self.current)

    def shutdown_snapshot(self, owner, executor, callback, startup=None):
        return dict(closed=owner._closed.is_set(), error=owner._close_error,
            session_closed=owner._session is None or owner._session.closed,
            ws_closed=owner._ws is None or owner._ws.closed,
            executor_released=owner._executor is None,
            executor_shutdown=executor is None or executor._shutdown,
            live_workers=sum(thread.is_alive() for thread in executor._threads) if executor else 0,
            future_drained=owner._http_future is None or owner._http_future.done(),
            tasks=len(owner.tasks), startup_done=startup is None or startup.done(),
            callback_gone=callback is None or not callback.exists(),
            bridge_stopped=owner.bridge._stopped, bridge_server_closed=owner.bridge._server is None,
            bridge_tasks=len(owner.bridge._tasks | owner.bridge._outbound),
            bridge_writers=len(owner.bridge._writers), bridge_pending=len(owner.bridge._pending),
            bridge_idle=len(owner.bridge._idle), holder=owner.holder() is not None,
            native_writes=self.frames.qsize())

    def assert_start_exit(self, value, *, cancelled):
        if cancelled:
            self.assertIsInstance(value, asyncio.CancelledError)
        else:
            self.assertIsInstance(value, api.ClientError,
                'close interrupting startup must refuse start(), not cancel an uncancelled caller')
            self.assertIsNone(value.__context__, 'refusal keeps no raw context')

    def assert_shutdown_quiet(self, snapshot):
        self.assertEqual(snapshot, dict(closed=True, error=None, session_closed=True, ws_closed=True,
            executor_released=True, executor_shutdown=True, live_workers=0, future_drained=True,
            tasks=0, startup_done=True, callback_gone=True, bridge_stopped=True,
            bridge_server_closed=True, bridge_tasks=0, bridge_writers=0, bridge_pending=0,
            bridge_idle=0, holder=False, native_writes=0))

    async def http_close_race(self, phase, *, cancel_start=False, cancel_close=False):
        owner = self.owner
        entered, release = self.http_entered[phase], self.http_release[phase]
        entered.clear(); release.clear()
        waiting = asyncio.Event()
        wrap = asyncio.wrap_future
        watched, wraps = None, 0
        def observe(future, *args, **kwargs):
            nonlocal wraps
            if future is watched:
                wraps += 1
                waiting.set()
            return wrap(future, *args, **kwargs)
        import aiohttp
        session_factory = aiohttp.ClientSession
        created_after_close = []
        def session(*args, **kwargs):
            created_after_close.append(owner._closing)
            return session_factory(*args, **kwargs)
        start = asyncio.create_task(owner.start())
        await asyncio.wait_for(entered.wait(), 3)
        executor, callback = owner._executor, owner.bridge._callback
        watched = owner._http_future
        self.assertIsNotNone(watched)
        self.assertFalse(watched.done(), 'barrier must own the actual unfinished HTTP worker')
        self.assertEqual(sum(thread.is_alive() for thread in executor._threads), 1)
        close = None
        try:
            with patch.object(asyncio, 'wrap_future', observe), patch.object(aiohttp, 'ClientSession', session):
                if cancel_start:
                    start.cancel()
                else:
                    close = asyncio.create_task(owner.aclose())
                await asyncio.wait_for(waiting.wait(), 3)
                premature = start.done() if cancel_start else close.done()
                self.assertIsNone(owner.holder())
                self.assertFalse(self.frames.qsize())
                if cancel_close:
                    close.cancel()
                    await asyncio.gather(close, return_exceptions=True)
                    close = asyncio.create_task(owner.aclose())
                release.set()
                results = await asyncio.wait_for(asyncio.gather(start,
                    *([close] if close is not None else []), return_exceptions=True), 10)
                repeats = await asyncio.wait_for(asyncio.gather(owner.aclose(), owner.wait_closed(),
                    return_exceptions=True), 10)
                snapshot = self.shutdown_snapshot(owner, executor, callback, start)
                evidence('registration-close resources:', dict(phase=phase,
                    cancel_start=cancel_start, cancel_close=cancel_close,
                    exits=[type(value).__name__ for value in results],
                    repeat_exits=[type(value).__name__ for value in repeats],
                    allocations_after_close=created_after_close, **snapshot))
            self.assertFalse(premature, 'successful drain cannot precede actual worker completion')
            self.assertGreaterEqual(wraps, 1)
            self.assertTrue(all(value is None for value in repeats), repeats)
            if close is not None:
                self.assertIsNone(results[-1], results)
            # Отменённый вызывающий получает свой cancel; startup, прерванный
            # чужим close, — отказ, а не CancelledError у неотменённой задачи.
            self.assert_start_exit(results[0], cancelled=cancel_start)
            self.assertFalse(any(created_after_close), 'startup allocated a session after closing')
            self.assert_shutdown_quiet(snapshot)
        finally:
            release.set()
            await asyncio.gather(start, *([close] if close is not None else []), return_exceptions=True)
            await self.repair_startup_fixture(owner, executor)

    async def test_registration_close_actual_tls_worker_three_reproductions(self):
        # Три независимые регистрации: исходный None.done и поздняя session
        # наблюдаются до любого repair. Никакого native acceptance здесь нет.
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                await self.http_close_race('registration')
        self.assertEqual(self.http_calls, dict(capabilities=3, registration=3))
        self.assertEqual(self.connected, 0)

    async def test_close_during_actual_capability_http_worker(self):
        await self.http_close_race('capabilities')
        self.assertEqual(self.http_calls, dict(capabilities=1, registration=0))

    async def test_start_cancellation_during_actual_registration_retains_worker(self):
        await self.http_close_race('registration', cancel_start=True)

    async def test_cancelled_close_waiter_cannot_abandon_registration_cleanup(self):
        await self.http_close_race('registration', cancel_close=True)

    async def test_cancel_after_registration_before_wss_still_owns_startup(self):
        owner = self.owner
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        real_http = owner._http
        completed = 0
        async def completed_http(function):
            nonlocal completed
            value = await real_http(function)
            completed += 1
            if completed == 2:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    # Реальный HTTP уже закончен. Даже кооперативный finally,
                    # уступивший loop после cancel, не должен создать session.
                    cancelled.set()
                    await release.wait()
            return value
        with patch.object(owner, '_http', completed_http):
            start = asyncio.create_task(owner.start())
            await asyncio.wait_for(entered.wait(), 3)
            executor, callback = owner._executor, owner.bridge._callback
            start.cancel()
            await asyncio.wait_for(cancelled.wait(), 3)
            close = asyncio.create_task(owner.aclose())
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.wait_for(asyncio.gather(start, close, return_exceptions=True), 10)
        snapshot = self.shutdown_snapshot(owner, executor, callback, start)
        evidence('post-registration cancellation resources:', dict(
            exits=[type(value).__name__ for value in results], **snapshot))
        try:
            self.assertIsNone(results[1], results)
            self.assert_start_exit(results[0], cancelled=True)
            self.assertIsNone(owner._session, 'no session may be created beyond the closing fence')
            self.assertEqual(self.connected, 0)
            self.assert_shutdown_quiet(snapshot)
        finally:
            await self.repair_startup_fixture(owner, executor)

    async def test_native_startup_close_interleavings_do_not_allocate_after_sweep(self):
        for phase in ('native_ready', 'callback_ready', 'bound_native_ready'):
            with self.subTest(phase=phase):
                owner = self.owner
                entered, release, cleaning = asyncio.Event(), asyncio.Event(), asyncio.Event()
                name = 'start' if phase == 'callback_ready' else 'ready'
                real_operation, real_close = getattr(owner.bridge, name), owner.bridge.aclose
                calls = 0
                async def operation():
                    nonlocal calls
                    result = await real_operation()
                    calls += 1
                    if calls == (2 if phase == 'bound_native_ready' else 1):
                        entered.set()
                        await release.wait()
                    return result
                async def observed_close():
                    cleaning.set()
                    await real_close()
                start = close = executor = None
                try:
                    with patch.object(owner.bridge, name, operation), patch.object(owner.bridge, 'aclose', observed_close):
                        start = asyncio.create_task(owner.start())
                        await asyncio.wait_for(entered.wait(), 3)
                        executor, callback = owner._executor, owner.bridge._callback
                        close = asyncio.create_task(owner.aclose())
                        await asyncio.wait_for(cleaning.wait(), 3)
                        release.set()
                        results = await asyncio.wait_for(asyncio.gather(start, close, return_exceptions=True), 10)
                    executor = executor or owner._executor
                    snapshot = self.shutdown_snapshot(owner, executor, callback, start)
                    evidence('native startup close resources:', dict(phase=phase,
                        exits=[type(value).__name__ for value in results], **snapshot))
                    self.assertIsNone(results[1], results)
                    self.assert_start_exit(results[0], cancelled=False)
                    self.assert_shutdown_quiet(snapshot)
                finally:
                    release.set()
                    await asyncio.gather(*(task for task in (start, close) if task is not None), return_exceptions=True)
                    await self.repair_startup_fixture(owner, executor or owner._executor)

    async def test_actual_wss_startup_close_interleavings(self):
        for phase, entered, release in (('handshake', self.ws_entered, self.ws_release),
                ('binding', self.bound_entered, self.bound_release)):
            with self.subTest(phase=phase):
                owner = self.owner
                entered.clear(); release.clear()
                cleaning = asyncio.Event()
                real_close = owner.bridge.aclose
                async def observed_close():
                    cleaning.set()
                    await real_close()
                start = close = executor = None
                try:
                    with patch.object(owner.bridge, 'aclose', observed_close):
                        start = asyncio.create_task(owner.start())
                        await asyncio.wait_for(entered.wait(), 3)
                        executor, callback = owner._executor, owner.bridge._callback
                        close = asyncio.create_task(owner.aclose())
                        await asyncio.wait_for(cleaning.wait(), 3)
                        release.set()
                        results = await asyncio.wait_for(asyncio.gather(start, close, return_exceptions=True), 10)
                    snapshot = self.shutdown_snapshot(owner, executor, callback, start)
                    evidence('WSS startup close resources:', dict(phase=phase,
                        exits=[type(value).__name__ for value in results], **snapshot))
                    self.assertIsNone(results[1], results)
                    self.assert_start_exit(results[0], cancelled=False)
                    self.assert_shutdown_quiet(snapshot)
                finally:
                    release.set()
                    await asyncio.gather(*(task for task in (start, close) if task is not None), return_exceptions=True)
                    await self.repair_startup_fixture(owner, executor)

    async def test_node_report_authority_cannot_own_or_borrow_human_receiver(self):
        from mop.cli import _mcp
        scope = api.CapabilityScope(project_id=self.project, client_id=self.client_id)
        owner = self.owner
        await owner.start()
        holder, callback, session = owner.holder(), owner.bridge._callback, owner._session
        self.actor = dict(user_id=2, kind='service', human_owner_id=None, node_id=key(), grant_id=None)
        with self.assertRaises(api.ClientError):
            await asyncio.to_thread(_mcp.Management, self.context, scope, inbox=owner)
        self.assertIs(owner.holder(), holder, 'a refused borrower cannot tear down the launcher owner')
        self.assertIs(owner._session, session)
        self.assertFalse(session.closed)
        self.assertTrue(callback.exists())
        other = self.module.InboxOwner(self.context, self.own, client_id=self.client_id,
            project_id=self.project, display_name='node cannot receive', current=lambda: self.current)
        try:
            with self.assertRaises(api.ClientError):
                await other.start()
            self.assertIsNone(other.endpoint)
            self.assertIsNone(other.holder())
            self.assertIsNone(other._executor)
            self.assertFalse(other.tasks)
            self.assertEqual(self.http_calls['registration'], 1, 'Node cannot register another receiver')
        finally:
            await other.aclose()
        self.assertFalse(_mcp.PRIVATE_INBOX['available'])
        self.assertIs(owner.holder(), holder)

    async def test_close_before_start_cannot_allocate_any_resource(self):
        owner = self.owner
        await owner.aclose()
        with self.assertRaises((api.ClientError, RuntimeError)):
            await owner.start()
        self.assert_shutdown_quiet(self.shutdown_snapshot(owner, None, None))
        self.assertEqual(self.http_calls, dict(capabilities=0, registration=0))

    async def test_unexpected_executor_cleanup_error_is_retained_on_every_waiter(self):
        owner = self.owner
        await owner.start()
        executor, callback = owner._executor, owner.bridge._callback
        real_shutdown = executor.shutdown
        def failing_shutdown(*args, **kwargs):
            real_shutdown(wait=True, cancel_futures=True)
            raise OSError('fixture-private-token')
        try:
            with patch.object(executor, 'shutdown', failing_shutdown):
                first = await asyncio.gather(owner.aclose(), return_exceptions=True)
            later = await asyncio.gather(owner.aclose(), owner.wait_closed(), return_exceptions=True)
            snapshot = self.shutdown_snapshot(owner, executor, callback)
            evidence('unexpected cleanup failure resources:', dict(
                exits=[type(value).__name__ for value in [*first, *later]], **snapshot))
            for error in [*first, *later]:
                self.assertIsInstance(error, RuntimeError)
                self.assertNotIn('fixture-private-token', str(error))
                self.assertIsNone(error.__context__)
                self.assertEqual(str(error), owner._close_error)
            self.assertTrue(owner._close_error)
            self.assertEqual(owner.failure, owner._close_error)
            self.assertEqual(snapshot['live_workers'], 0)
            self.assertTrue(snapshot['session_closed'])
            self.assertTrue(snapshot['callback_gone'])
            self.assertFalse(snapshot['holder'])
        finally:
            await self.repair_startup_fixture(owner, executor)

    async def test_close_step_without_effect_is_not_successful_closure(self):
        # HYPOTHESIS (#786): success выводится из отсутствия исключения; шаг,
        # вернувшийся без эффекта, оставляет live callback/session за closed.
        for name, attribute in (('native bridge', 'aclose'), ('HTTPS session', 'close')):
            with self.subTest(resource=name):
                owner = self.owner
                await owner.start()
                executor, callback = owner._executor, owner.bridge._callback
                target = owner.bridge if name == 'native bridge' else owner._session
                async def without_effect():
                    return None
                try:
                    with patch.object(target, attribute, without_effect):
                        first = await asyncio.gather(owner.aclose(), return_exceptions=True)
                    later = await asyncio.gather(owner.aclose(), owner.wait_closed(), return_exceptions=True)
                    snapshot = self.shutdown_snapshot(owner, executor, callback)
                    evidence('close without effect resources:', dict(resource=name,
                        exits=[type(value).__name__ for value in [*first, *later]], **snapshot))
                    for error in [*first, *later]:
                        self.assertIsInstance(error, RuntimeError, 'a live resource is not successful closure')
                        self.assertEqual(str(error), owner._close_error)
                    self.assertIn(name, owner._close_error)
                    self.assertEqual(owner.failure, owner._close_error)
                    self.assertFalse(snapshot['holder'])
                    self.assertEqual(snapshot['live_workers'], 0)
                    self.assertEqual(snapshot['native_writes'], 0)
                finally:
                    await self.repair_startup_fixture(owner, executor)

    async def test_queued_http_worker_cancel_is_joined_before_close_success(self):
        # HYPOTHESIS: cancel_futures=True отменяет ещё queued admission, но
        # initializer настоящего executor остаётся живым до release.
        owner = self.owner
        loop = asyncio.get_running_loop()
        entered, shutting_down = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        real_executor = self.module.ThreadPoolExecutor
        def executor_factory(*args, **kwargs):
            def initialize():
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(10):
                    raise AssertionError('test did not release actual worker initializer')
            return real_executor(*args, initializer=initialize, **kwargs)
        start = close = executor = None
        try:
            with patch.object(self.module, 'ThreadPoolExecutor', executor_factory):
                start = asyncio.create_task(owner.start())
                await asyncio.wait_for(entered.wait(), 3)
                executor, callback = owner._executor, owner.bridge._callback
                future = owner._http_future
                self.assertFalse(future.running())
                real_shutdown = executor.shutdown
                def observed_shutdown(*args, **kwargs):
                    shutting_down.set()
                    return real_shutdown(*args, **kwargs)
                with patch.object(executor, 'shutdown', observed_shutdown):
                    close = asyncio.create_task(owner.aclose())
                    await asyncio.wait_for(shutting_down.wait(), 3)
                    self.assertFalse(close.done())
                    self.assertEqual(sum(thread.is_alive() for thread in executor._threads), 1)
                    release.set()
                    results = await asyncio.wait_for(asyncio.gather(start, close, return_exceptions=True), 10)
            snapshot = self.shutdown_snapshot(owner, executor, callback, start)
            evidence('queued HTTP cancellation resources:', dict(
                exits=[type(value).__name__ for value in results], **snapshot))
            self.assertTrue(future.cancelled())
            self.assertIsNone(results[1], results)
            self.assert_start_exit(results[0], cancelled=False)
            self.assert_shutdown_quiet(snapshot)
            self.assertEqual(self.http_calls, dict(capabilities=0, registration=0))
        finally:
            release.set()
            await asyncio.gather(*(task for task in (start, close) if task is not None), return_exceptions=True)
            await self.repair_startup_fixture(owner, executor)

    async def test_actual_worker_beyond_drain_budget_cannot_report_success(self):
        owner = self.owner
        release, returned = threading.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        real_register = api.Client.register_endpoint
        def late_register(client, **kwargs):
            value = real_register(client, **kwargs)
            loop.call_soon_threadsafe(entered.set)
            try:
                if not release.wait(10):
                    raise AssertionError('test did not release its actual registration worker')
                return value
            finally:
                returned.set()
        executor = None
        start = None
        try:
            with patch.object(api.Client, 'register_endpoint', late_register):
                start = asyncio.create_task(owner.start())
                await asyncio.wait_for(entered.wait(), 3)
                executor, callback = owner._executor, owner.bridge._callback
                began = time.monotonic()
                result = await asyncio.wait_for(asyncio.gather(owner.aclose(), return_exceptions=True), 10)
                elapsed = time.monotonic()-began
                snapshot = self.shutdown_snapshot(owner, executor, callback)
                repeats = await asyncio.gather(owner.aclose(), owner.wait_closed(), return_exceptions=True)
                evidence('bounded late worker resources:', dict(elapsed=round(elapsed, 3),
                    exits=[type(value).__name__ for value in [*result, *repeats]], **snapshot))
                self.assertLess(elapsed, 10)
                self.assertFalse(returned.is_set(), 'test must observe a real worker still inside registration')
                self.assertEqual(snapshot['live_workers'], 1)
                self.assertTrue(snapshot['closed'])
                self.assertTrue(snapshot['error'], 'a live worker is not successful closure')
                for error in [*result, *repeats]:
                    self.assertIsInstance(error, RuntimeError)
                    self.assertEqual(str(error), owner._close_error)
                self.assertFalse(snapshot['holder'])
                self.assertTrue(snapshot['session_closed'])
                self.assertEqual(snapshot['native_writes'], 0)
                release.set()
                start_exit, = await asyncio.wait_for(asyncio.gather(start, return_exceptions=True), 5)
                self.assert_start_exit(start_exit, cancelled=False)
                deadline = loop.time()+2
                while any(thread.is_alive() for thread in executor._threads) and loop.time() < deadline:
                    await asyncio.sleep(.01)
                self.assertTrue(returned.is_set())
                self.assertTrue(executor._shutdown, 'even failed drain must arrange eventual worker exit')
                self.assertFalse(any(thread.is_alive() for thread in executor._threads))
                with self.assertRaisesRegex(RuntimeError, 'shutdown incomplete'):
                    await owner.wait_closed()
        finally:
            release.set()
            if start is not None:
                await asyncio.gather(start, return_exceptions=True)
            await self.repair_startup_fixture(owner, executor)

    async def test_actual_wss_guard_one_native_write_and_matched_idle_after_receipt(self):
        self.assertIsNone(self.owner.holder())
        await self.owner.start()
        self.assertIsNotNone(self.owner.holder())
        frame = await self.delivery(True)
        accepted = await asyncio.wait_for(self.evidence.get(), 3)
        self.assertEqual(accepted['outcome'], 'accepted')
        self.assertEqual(accepted['message_id'], frame['message_id'])
        idle = await asyncio.wait_for(self.evidence.get(), 3)
        self.assertEqual(idle['type'], 'idle_observed')
        native = [await self.frames.get(), await self.frames.get()]
        self.assertEqual([x['type'] for x in native], ['user', 'control'])
        self.assertTrue(all(x['session_id'] == self.external for x in native))
        self.assertEqual(sum(x['type']=='prewrite' for x in self.wire_seen), 1)
        await self.delivery(**frame)
        await asyncio.wait_for(self.owner.wait_closed(), 3)
        self.assertTrue(self.frames.empty(), 'duplicate native input must never replay')

    async def test_two_mcp_consumers_share_injected_owner_without_starting_receivers(self):
        from mop.cli import _mcp
        import inspect
        self.assertIn('inbox', inspect.signature(_mcp.Management).parameters,
            'trusted optional inbox port is missing from shared management composition')
        scope = api.CapabilityScope(project_id=self.project, client_id=self.client_id)
        await self.owner.start()
        callback = self.owner.bridge._callback
        first = await asyncio.to_thread(_mcp.Management, self.context, scope, inbox=self.owner)
        second = await asyncio.to_thread(_mcp.Management, self.context, scope, inbox=self.owner)
        self.assertTrue(first.private_status()['available'])
        for management in (first, second):
            result = await asyncio.to_thread(management.call, 'report', dict(address='private:'+key(),
                text='private-marker', epoch=self.epoch, message_id=key()))
            self.assertFalse(result.model_dump(by_alias=True)['isError'], result)
            self.assertEqual(json.loads(result.content[0].text)['delivery']['state'], 'queued')
        self.assertEqual(self.connected, 1)
        self.assertEqual(self.owner.bridge._callback, callback)
        self.assertEqual(len(self.report_headers), 2)
        self.assertTrue(all(row['X-Mop-Connection'] == self.nonce and row['X-Mop-Endpoint'] == self.endpoint
            for row in self.report_headers))
        self.current = False
        self.assertFalse(first.private_status()['available'])
        result = await asyncio.to_thread(first.call, 'report', dict(address='private:'+key(),
            text='private-marker', epoch=self.epoch, message_id=key()))
        self.assertTrue(result.model_dump(by_alias=True)['isError'])
        self.assertEqual(len(self.report_headers), 2, 'stale injected holder must not silently downgrade')
        self.assertFalse(_mcp.PRIVATE_INBOX['available'])

    async def test_agent_with_report_action_cannot_acquire_human_receiver(self):
        self.actor = dict(user_id=2, kind='agent', human_owner_id=1, node_id=None, grant_id=key())
        with self.assertRaises(api.ClientError):
            await self.owner.start()
        self.assertIsNone(self.owner.endpoint)
        self.assertIsNone(self.owner.holder())
        self.assertEqual(self.connected, 0)

    async def test_disconnect_after_native_acceptance_before_receipt_never_replays_or_sends_idle(self):
        self.drop_receipt = True
        await self.owner.start()
        await self.delivery(True)
        evidence = await asyncio.wait_for(self.evidence.get(), 3)
        self.assertEqual(evidence['outcome'], 'accepted')
        await asyncio.wait_for(self.owner.wait_closed(), 3)
        self.assertEqual(sum(value['type'] == 'user' for value in list(self.frames._queue)), 1)
        self.assertFalse(any(value['type'] == 'idle_observed' for value in self.wire_seen))
        with self.assertRaises(RuntimeError):
            await self.owner.start()
        self.assertIsNone(self.owner.holder())

    async def test_wrong_physical_conversation_frame_never_reaches_native(self):
        await self.owner.start()
        await self.delivery(external_session_id='another-owned-conversation')
        await asyncio.wait_for(self.owner.wait_closed(), 3)
        self.assertTrue(self.frames.empty())

    async def test_clear_during_awaited_prewrite_never_writes(self):
        await self.owner.start()
        self.permit.clear()
        await self.delivery()
        for _ in range(200):
            if self.wire_seen: break
            await asyncio.sleep(.01)
        self.assertEqual(self.wire_seen[0]['type'], 'prewrite')
        self.current = False
        self.assertIsNone(self.owner.holder())
        await asyncio.wait_for(self.owner.wait_closed(), 3)
        self.permit.set()
        self.assertTrue(self.frames.empty())

    async def test_native_held_and_eof_are_unknown_not_accepted_or_idle(self):
        self.native_mode = 'held'
        await self.owner.start()
        await self.delivery(True)
        evidence = await asyncio.wait_for(self.evidence.get(), 4)
        self.assertEqual(evidence['outcome'], 'unknown')
        self.assertTrue(self.frames.qsize() == 1)
        self.assertFalse(any(x['type']=='idle_observed' for x in self.wire_seen))
        await self.socket.close()
        await asyncio.wait_for(self.owner.wait_closed(), 3)
        self.assertIsNone(self.owner.holder())

    async def test_lifetime_capacity_closes_generation_after_final_idle_without_evicting_ids(self):
        await self.owner.start()
        for index in range(64):
            await self.delivery(index == 63)
            value = await asyncio.wait_for(self.evidence.get(), 3)
            self.assertEqual(value['outcome'], 'accepted')
            while self.owner._delivery_task is not None and not self.owner._delivery_task.done():
                await asyncio.sleep(.001)
        idle = await asyncio.wait_for(self.evidence.get(), 3)
        self.assertEqual(idle['type'], 'idle_observed')
        await asyncio.wait_for(self.owner.wait_closed(), 3)
        self.assertIsNone(self.owner.holder())
        self.assertEqual(len([x for x in self.wire_seen if x['type']=='prewrite']), 64)

    async def test_binary_or_unsolicited_receipt_closes_no_native_input(self):
        await self.owner.start()
        await self.socket.send_bytes(b'{}')
        await asyncio.wait_for(self.owner.wait_closed(), 3)
        self.assertTrue(self.frames.empty())
        self.assertIsNone(self.owner.holder())

    async def test_holder_cannot_be_borrowed_by_another_actor_or_server(self):
        await self.owner.start()
        holder = self.owner.holder()
        foreign = api.Client(api.ServerContext(self.context.origin, 'other-actor-token', str(self.cert)))
        with self.assertRaises(api.ClientError):
            foreign._holder_headers(holder)
        self.assertEqual(api.Client(self.context)._holder_headers(holder)['X-Mop-Endpoint'], self.endpoint)
        self.current = False
        with self.assertRaises(api.ClientError):
            api.Client(self.context)._holder_headers(holder)

    async def test_same_owner_start_is_not_second_receiver_or_new_registration(self):
        await self.owner.start()
        with self.assertRaises(RuntimeError):
            await self.owner.start()
        self.assertEqual(self.connected, 1)
        self.assertIsNotNone(self.owner.holder())
        self.assertNotIn(self.nonce, repr(self.owner.holder()))
        self.assertNotIn('fixture-private-token', repr(self.owner))

    async def test_overlapping_shutdown_waits_for_same_uninterrupted_cleanup(self):
        await self.owner.start()
        entered, release = asyncio.Event(), asyncio.Event()
        real_close = self.owner.bridge.aclose
        interrupted = False
        async def blocked_close():
            nonlocal interrupted
            entered.set()
            try:
                await release.wait()
                await real_close()
            except asyncio.CancelledError:
                interrupted = True
                raise
        with patch.object(self.owner.bridge, 'aclose', blocked_close):
            first = asyncio.create_task(self.owner.aclose())
            await asyncio.wait_for(entered.wait(), 3)
            second = asyncio.create_task(self.owner.aclose())
            await asyncio.sleep(.05)
            premature = second.done()
            release.set()
            results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 3)
        # HYPOTHESIS: второй close отменяет runner внутри его finally.
        self.assertFalse(interrupted, 'overlapping close interrupted owned resource cleanup')
        self.assertFalse(premature, 'close returned before physical resource cleanup')
        self.assertTrue(all(value is None for value in results), results)
        self.assertTrue(self.owner._session.closed)

    async def test_one_close_failure_does_not_abandon_other_owned_resources(self):
        await self.owner.start()
        real_close = self.owner.bridge.aclose
        async def failing_close():
            await real_close()
            raise OSError('fixture-private-token')
        with patch.object(self.owner.bridge, 'aclose', failing_close):
            with self.assertRaises(RuntimeError) as caught:
                await self.owner.aclose()
        self.assertNotIn('fixture-private-token', str(caught.exception))
        self.assertTrue(self.owner._session.closed)
        self.assertIsNone(self.owner._executor)
        self.assertIsNone(self.owner.holder())
        self.owner._close_error = None

    async def test_raw_startup_exception_does_not_survive_safe_diagnostic(self):
        import aiohttp
        async def unavailable_socket(*args, **kwargs):
            raise OSError('fixture-private-token')
        with patch.object(aiohttp.ClientSession, 'ws_connect', unavailable_socket):
            with self.assertRaises(api.ClientError) as caught:
                await self.owner.start()
        self.assertNotIn('fixture-private-token', str(caught.exception))
        self.assertIsNone(caught.exception.__context__, 'raw transport error must not remain reachable')
        self.assertIsNone(self.owner.holder())
        self.assertTrue(self.owner._session.closed)

    async def test_tls_verification_cannot_fall_back_to_system_or_http(self):
        self.owner.context = api.ServerContext(self.context.origin, self.context.token)
        with self.assertRaises(api.ClientError):
            await self.owner.start()
        self.assertEqual(self.connected, 0)
        self.assertIsNone(self.owner.holder())

    async def test_native_loss_probe_and_shutdown_clear_holder_and_callback(self):
        await self.owner.start()
        self.native.close(); await self.native.wait_closed()
        Path(self.native_path).unlink()
        await self.socket.send_str(inbox_encode(dict(schema_version=1, type='probe',
            connection_nonce=self.nonce, probe_id=key())))
        await asyncio.wait_for(self.owner.wait_closed(), 3)
        self.assertIsNone(self.owner.holder())


if __name__ == '__main__':
    unittest.main()
