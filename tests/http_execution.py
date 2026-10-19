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
from datetime import datetime, timezone
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

import _lib  # noqa: F401 -- общий временный корень процесса (#338)
import hermetic  # noqa: F401
from _body_fixture import wire
from mop.node.http_execution import MachineClient, Runtime, Work, Refused, Unavailable, HttpsTransport, EffectGuard
from mop.node.execution_adapters import BoundTarget, ActionInput, LocalExecutor, ProcessRunner, UnixSessionSender
from mop.node.execution_facts import LocalFacts, FactScope


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
    # FEATURE: addressed identity input и предыдущий epoch приходят с сервера.
    # HYPOTHESIS: прежний client имеет лишь metadata и вынуждает guessing/bypass.
    # RED: отсутствовал addressed input client; boot передавал None вместо query.
    # SOLUTION: strict finite DTO и явный server epoch query перед restart CAS.
    # RESULT: node/pure suite green, native HTTPS proof отдельно в controller gate.
    # STATUS: FIXED — see #625
    def test_addressed_input_and_executor_discovery_have_strict_finite_shapes(self):
        work = Work.parse(wire(action='apply_identity'))
        class InputTransport:
            value = dict(schema_version=1,operation_id=work.operation_id,step_id=work.step_id,
                git_name='Owner',git_email='owner@example.invalid')
            async def request(inner,method,path,payload=None,params=None):
                return inner.value
        transport = InputTransport()
        client = MachineClient(transport)
        self.assertTrue(callable(getattr(client,'action_input',None)), 'addressed input client port missing')
        self.assertEqual(asyncio.run(client.action_input(work))['git_name'],'Owner')
        transport.value = {**transport.value,'shell':'id'}
        with self.assertRaises(Refused):
            asyncio.run(client.action_input(work))
        self.assertTrue(callable(getattr(client,'executor_state',None)), 'server epoch discovery client port missing')
        transport.value = dict(schema_version=1, installation_id='fixture', epoch=work.epoch,
            node_id=work.node_id, executor_epoch=work.executor_epoch)
        self.assertEqual(asyncio.run(client.executor_state(work.node_id)), transport.value)
        transport.value = {**transport.value,'node_id':str(uuid4())}
        with self.assertRaises(Refused):
            asyncio.run(client.executor_state(work.node_id))

    def test_initial_create_vocabulary_is_finite_without_binding_physical_ports(self):
        # HYPOTHESIS #721: server native_exec не проходит общий wire consumer.
        # SOLUTION: общий конечный словарь, без включения physical ports.
        # RESULT: native_exec/initial endpoint были refused; wire checks green.
        # STATUS: FIXED — see #721; unknown fallback по-прежнему запрещён.
        from mop.common.initialcreate import MACHINE, CONFIRMATIONS
        from mop.node.http_execution import EVIDENCE
        from mop.node.execution_adapters import LocalExecutor
        for action in MACHINE:
            work = Work.parse(wire(action=action))
            self.assertEqual(Work.parse(work.wire()), work)
            with self.assertRaises(Unavailable):
                asyncio.run(LocalExecutor(None, None).execute(work, None))
        self.assertTrue(set(CONFIRMATIONS.values()) <= EVIDENCE)
        with self.assertRaises(Refused):
            Work.parse(wire(action='arbitrary_new_action'))

    def test_strict_finite_work(self):
        self.assertEqual(Work.parse(wire()).action, 'send_message')
        for change in ({'action':'shell'}, {'owner_id':1}, {'revision':'01'},
                       {'node_id':'not-a-uuid'}, {'not_after':'2030-01-01'},
                       {'schema_version':True}, {'argv':['rm']}, {'role':'../worker'}):
            with self.subTest(change=change), self.assertRaises(Refused):
                Work.parse(wire(**change))


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_boot_without_previous_epoch_queries_server_before_restart_cas(self):
        work = wire()
        previous = str(uuid4())
        class Discovery(Transport):
            async def request(inner,method,path,payload=None,params=None):
                if path == 'executor':
                    inner.calls.append((method,path,payload,params))
                    return dict(schema_version=1, installation_id='fixture', epoch=work['epoch'],
                        node_id=work['node_id'], executor_epoch=previous)
                return await super().request(method,path,payload,params)
        transport = Discovery(work)
        runtime = Runtime(MachineClient(transport),Executor(),node_id=work['node_id'],epoch=work['epoch'],
            executor_epoch=work['executor_epoch'])
        try:
            await runtime.start(None)
            self.assertEqual([call[1] for call in transport.calls],['executor','executor/restarted'])
            self.assertEqual(transport.calls[1][2]['previous_epoch'],previous)
        finally:
            await runtime.aclose()

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

    async def test_unexpected_adapter_failure_reports_unknown_without_raw_secret(self):
        async def broken(work, guard):
            await guard()
            raise RuntimeError('private credential in exception')
        self.executor.execute = broken
        await self.runtime.tick()
        await self.runtime.drain()
        self.assertEqual(self.transport.calls[-1][1], 'work/result')
        self.assertEqual(self.transport.calls[-1][2]['outcome'], 'unknown')
        self.assertNotIn('private credential', self.runtime.last_error)

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
    async def test_parallel_fact_pages_keep_their_reserved_observation_sequence(self):
        work = Work.parse(wire())
        root = Path('/tmp/mop640-fake-facts')
        target = BoundTarget(work.target_id, work.incarnation_id, work.node_id,
            work.executor_epoch, work.generation, work.role, work.bundle_digest,
            work.grant_id, root, root/'tmux', '%0', root/'launcher')
        entered, release = asyncio.Event(), asyncio.Event()
        class Authority:
            async def authorize(self, node_id, kind, limit):
                return FactScope(node_id, kind, (target,), True)
        facts = LocalFacts(None, Authority())
        async def values(kind, physical):
            if not entered.is_set():
                entered.set()
                await release.wait()
            return {'healthy':True,'latency_ms':1}
        facts._values = values
        first = asyncio.create_task(facts.read(node_id=work.node_id, kind='health'))
        await entered.wait()
        second = await facts.read(node_id=work.node_id, kind='health')
        release.set()
        first_page = await first
        self.assertEqual(first_page['items'][0]['sequence'], '1')
        self.assertEqual(second['items'][0]['sequence'], '2')

    async def test_fact_timeout_is_explicit_unavailable_not_empty_or_raw_timeout(self):
        class Authority:
            async def authorize(self, *args): await asyncio.Event().wait()
        runner = ProcessRunner()
        facts = LocalFacts(runner, Authority(), timeout=.1)
        try:
            with self.assertRaises(Unavailable):
                await facts.read(node_id=str(uuid4()), kind='junk')
        finally:
            await runner.aclose()

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
                self.assertTrue(facts.last_error, 'unavailable source reason must remain observable')
            denied = LocalFacts(runner, None)
            with self.assertRaises(Unavailable):
                await denied.read(node_id=work.node_id, kind='junk', limit=1)
            self.assertEqual(reads, ['clone','clone','disk','sizes','states','usage','junk'])
        await runner.aclose()


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_http_entry_owns_client_even_when_reconciliation_fails(self):
        from mop.node import http_execution
        value = wire()
        transport = Transport(value)
        class Resolver:
            async def reconcile(self, *args): return False
        with patch.object(http_execution, 'HttpsTransport', return_value=transport):
            with self.assertRaises(Unavailable):
                await http_execution.run_http_agent(origin='https://controller.test', token='credential',
                    node_id=value['node_id'], epoch=value['epoch'], executor_epoch=value['executor_epoch'],
                    previous_epoch=str(uuid4()), resolver=Resolver())
        self.assertTrue(transport.closed)

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

    async def test_addressed_launch_uses_finite_post_without_disclosing_work_in_url(self):
        # HYPOTHESIS (#643): LaunchQuery ещё не связан с finite HTTPS transport.
        # SPEC: POST metadata body, тот же verified transport, не UUID oracle.
        # RED: missing LaunchQuery adapter; SOLUTION: явный POST в allowlist и
        # MachineClient.read_launch. RESULT: 30/30 green. STATUS: FIXED -- see #643.
        metadata, seen = wire(), []
        class Response:
            status = 200
            content_type = 'application/json'
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            @property
            def content(self): return self
            async def iter_chunked(self, size): yield b'{"schema_version":1}'
        class Session:
            def __init__(self, **kwargs): self.connector = kwargs['connector']
            def request(self, method, url, **kwargs):
                seen.append((method, url, kwargs))
                return Response()
            async def close(self): await self.connector.close()
        with patch('aiohttp.ClientSession', Session):
            transport = HttpsTransport('https://host.test', 'credential')
            client = MachineClient(transport)
            self.assertTrue(callable(getattr(client, 'read_launch', None)), 'LaunchQuery adapter is missing')
            try:
                value = await client.read_launch(metadata)
                self.assertEqual(value, {'schema_version': 1})
                self.assertEqual(seen[0][:2], ('POST', 'https://host.test/api/v1/machine/work/launch'))
                self.assertEqual(seen[0][2]['json'], metadata)
                self.assertIsNone(seen[0][2]['params'])
                with self.assertRaises(Refused): await transport.request('GET', 'work/launch')
                # #721: отдельный именованный POST, не generic delivery endpoint.
                await transport.request('POST', 'work/initial-launch', metadata)
                self.assertEqual(seen[1][:2], ('POST', 'https://host.test/api/v1/machine/work/initial-launch'))
                self.assertEqual(seen[1][2]['json'], metadata)
                self.assertIsNone(seen[1][2]['params'])
                for method, path in (('GET', 'work/initial-launch'), ('POST', 'work/initial-launch/secret')):
                    with self.assertRaises(Refused): await transport.request(method, path)
            finally:
                await client.aclose()

    async def test_management_context_is_finite_bound_secret_safe_read_not_launch_work(self):
        # HYPOTHESIS #742: нет strict typed post-activation клиента; scope не Work.
        # SPEC: exact POST body, no token/owner/path/origin authority, DTO repr safe.
        # SOLUTION: finite HTTPS method + strict common request/secret-safe DTO.
        # RESULT: missing client RED; 33/33 GREEN, mismatched scope/plaintext refused.
        # STATUS: FIXED — see #742.
        from datetime import timedelta
        from mop.common.management_context import ManagementRequest, ManagementContext
        from mop.common.telemetry import SessionScope
        request = ManagementRequest(SessionScope(uuid4(), uuid4(), uuid4(), uuid4(),
            uuid4(), 'native-session', uuid4()), 3)
        context = ManagementContext(request, 'installation-fixture', uuid4(), uuid4(), uuid4(), 12,
            uuid4(), 7, datetime.now(timezone.utc)+timedelta(minutes=2), 'e'*40)
        result, calls = context.wire(), []
        class Response:
            status = 200
            content_type = 'application/json'
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            @property
            def content(self): return self
            async def iter_chunked(self, size): yield json.dumps(result).encode()
        class Session:
            def __init__(self, **kwargs): self.connector = kwargs['connector']
            def request(self, method, url, **kwargs):
                calls.append((method, url, kwargs))
                return Response()
            async def close(self): await self.connector.close()
        with patch('aiohttp.ClientSession', Session):
            transport = HttpsTransport('https://host.test', 'node-credential')
            client = MachineClient(transport)
            self.assertTrue(callable(getattr(client, 'read_management_context', None)),
                'finite management context client is missing')
            try:
                received = await client.read_management_context(request.wire())
                self.assertEqual(received, context)
                self.assertNotIn(context.token, repr(received))
                self.assertEqual(calls[0][:2], ('POST', 'https://host.test/api/v1/machine/management-context'))
                self.assertEqual(calls[0][2]['json'], request.wire())
                self.assertIsNone(calls[0][2]['params'])
                for method, path in (('GET', 'management-context'), ('POST', 'management-context/token')):
                    with self.assertRaises(Refused): await transport.request(method, path)
                for key in ('token', 'actor_id', 'token_id', 'owner', 'origin', 'path', 'secret_name', 'work'):
                    with self.subTest(key=key), self.assertRaises((ValueError, Refused)):
                        await client.read_management_context({**request.wire(), key: 'untrusted'})
                for key, value in (('subject_id', True), ('token', 'invalid'), ('credential_id', 'bad'),
                        ('schema_version', True), ('assignment_revision', '0'), ('origin', 'https://foreign.invalid')):
                    with self.subTest(key=key), self.assertRaises(ValueError):
                        ManagementContext.parse({**result, key: value}, request)
                for name in ('target_id', 'incarnation_id', 'node_id', 'executor_epoch', 'producer_epoch', 'epoch'):
                    result['request'] = {**request.wire(), 'scope': {**request.scope.wire(), name: str(uuid4())}}
                    with self.subTest(name=name), self.assertRaises(Refused):
                        await client.read_management_context(request.wire())
                result['request'] = request.wire()
                result['token'] = 'response-plaintext-must-not-appear'
                with self.assertRaises(Refused) as failure:
                    await client.read_management_context(request.wire())
                self.assertNotIn(result['token'], str(failure.exception))
            finally:
                await client.aclose()

    # HYPOTHESIS (#717): transport выбрасывает non-200 до чтения canonical
    # error envelope, и причина безопасного отказа теряется на HTTP503.
    # SOLUTION: bounded canonical error body читается под прежним deadline;
    # safe phrase сохраняется, bearer redacted, raw/invalid body не отражается.
    # RESULT: RED -- safe reason отсутствует; GREEN -- owned TLS и node checks.
    # STATUS: FIXED — see #717
    async def test_owned_https_preserves_safe_drf_reason_without_echoing_token(self):
        import http.server
        from _control_fixture import own_tls

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.server.calls += 1
                raw = self.server.body
                self.send_response(self.server.status)
                self.send_header('Content-Type', self.server.content_type)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        token = 'owned-node-secret-not-in-errors'
        with own_tls(Handler) as (server, origin, cert, _):
            server.calls, server.content_type = 0, 'application/json'
            transport = HttpsTransport(origin, token, tls=ssl.create_default_context(cafile=str(cert)), response_limit=1024)
            try:
                for status, kind in ((403, Refused), (503, Unavailable)):
                    server.status = status
                    server.body = json.dumps({'schema_version': 1, 'error': {
                        'code': 'authority_unavailable', 'message': 'pinned bootstrap approval was revoked',
                        'retryable': False}}).encode()
                    with self.assertRaises(kind) as raised:
                        await transport.request('GET', 'work')
                    self.assertIn('pinned bootstrap approval was revoked', str(raised.exception))
                    self.assertIn('HTTP '+str(status), str(raised.exception))
                server.body = json.dumps({'schema_version': 1, 'error': {'code': 'authority_unavailable',
                    'message': 'credential '+token+' is unavailable', 'retryable': False}}).encode()
                with self.assertRaises(Unavailable) as raised:
                    await transport.request('GET', 'work')
                self.assertNotIn(token, str(raised.exception))
                self.assertIn('[redacted]', str(raised.exception))
                private = 'private-raw-error-not-in-diagnostics'
                bad = (
                    ('text/html', ('<html>'+private+'</html>').encode()),
                    ('application/json', json.dumps({'schema_version': 2, 'error': {
                        'code': 'authority_unavailable', 'message': private, 'retryable': True}}).encode()),
                    ('application/json', ('{"schema_version":1,"error":{"code":"x","message":"'+private+
                        '","message":"duplicate","retryable":true}}').encode()),
                    ('application/json', ('{"error":"'+private*100+'"}').encode()),
                    ('application/json', json.dumps({'schema_version': 1, 'error': {
                        'code': 'authority_unavailable', 'message': private+'\nraw traceback', 'retryable': True}}).encode()),
                )
                for content_type, raw in bad:
                    server.content_type, server.body = content_type, raw
                    with self.assertRaises(Unavailable) as raised:
                        await transport.request('GET', 'work')
                    self.assertNotIn(private, str(raised.exception))
                    self.assertNotIn(token, str(raised.exception))
                    self.assertIn('HTTP 503', str(raised.exception))
                self.assertEqual(server.calls, 3+len(bad), 'diagnostics must never retry requests')
            finally:
                await transport.aclose()

    async def test_absolute_canonical_codec_maximum_crosses_actual_verified_tls(self):
        # Transport boundary, не SQL admission: codec разрешает 32 refs,
        # тогда как SQL create резервирует один consumer slot для model_config.
        import base64
        import hashlib
        import http.server
        from uuid import uuid4
        from _control_fixture import own_tls
        from _host_inputs_data import selected
        from mop.common.bootstrap_envelope import bounded_content, encode, decode_base64
        a, _, value = selected('bootstrap_body')
        raw = encode(bounded_content(b'x'*262144, b'y'*131072, '\U0001f600'*1024,
            'a'*64, tuple(uuid4() for _ in range(32))))
        self.assertEqual(len(raw), 537974)
        encoded = base64.b64encode(raw).decode()
        self.assertEqual(len(encoded), 717300)
        value['bootstrap_digest'] = hashlib.sha256(raw).hexdigest()
        value['bootstrap'] = dict(envelope=encoded)
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                body = json.dumps(value, separators=(',', ':')).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        with own_tls(Handler) as (_, origin, cert, _):
            client = MachineClient(HttpsTransport(origin, 'owned-fixture-token',
                tls=ssl.create_default_context(cafile=str(cert))))
            try:
                delivered = await client.read_initial_launch(a.work.wire())
                self.assertEqual(encode(decode_base64(delivered['bootstrap']['envelope'], delivered['bootstrap_digest'])), raw)
            finally:
                await client.aclose()

    async def test_bootstrap_https_allowance_is_phase_specific_and_explicit_caps_stay_hard(self):
        import http.server
        from _control_fixture import own_tls
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                raw = json.dumps(dict(payload='x'*300000)).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            do_GET = do_POST
        with own_tls(Handler) as (_, origin, cert, _):
            for options, method, path, action, accepted in (
                ({}, 'POST', 'work/initial-launch', 'bootstrap_body', True),
                ({'response_limit': 262144}, 'POST', 'work/initial-launch', 'bootstrap_body', False),
                ({'response_limit': 1024}, 'POST', 'work/initial-launch', 'bootstrap_body', False),
                ({}, 'POST', 'work/initial-launch', 'native_exec', False),
                ({}, 'POST', 'work/launch', 'bootstrap_body', False),
                ({}, 'GET', 'work', 'bootstrap_body', False)):
                transport = HttpsTransport(origin, 'owned-fixture-token',
                    tls=ssl.create_default_context(cafile=str(cert)), **options)
                try:
                    self.assertEqual(transport.timeout, 10.)
                    if accepted:
                        self.assertEqual(len((await transport.request(method, path, payload=dict(action=action)))['payload']), 300000)
                    else:
                        with self.assertRaisesRegex(Refused, 'exceeds limit'):
                            await transport.request(method, path, payload=dict(action=action))
                finally:
                    await transport.aclose()

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
            from mop.driver.launch import Registration
            from mop.node.body_launch import SessionProof, process_identity
            registration = Registration(work, str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
            proof = SessionProof(registration, '%0', 'owned-session', uuid4(), socket,
                                 os.getpid(), process_identity(os.getpid()))
            target = BoundTarget(work.target_id, work.incarnation_id, work.node_id,
                work.executor_epoch, work.generation, work.role, work.bundle_digest,
                work.grant_id, Path.cwd(), root/'tmux', '%0', root/'launcher',
                session_socket=socket, session_proof=proof)
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

    async def test_runner_shutdown_owns_in_progress_spawn(self):
        runner = ProcessRunner()
        spawned, release = asyncio.Event(), asyncio.Event()
        children = []
        original = asyncio.create_subprocess_exec
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
                closing = asyncio.create_task(runner.aclose())
                await asyncio.sleep(.01)
                self.assertFalse(closing.done(), 'shutdown forgot a live spawn')
                release.set()
                await closing
                with self.assertRaises(Unavailable): await task
                self.assertIsNotNone(children[0].returncode)
        finally:
            release.set()
            for child in children:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            await runner.aclose()

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
            class Replacement:
                async def confirmed(self, work, target): return False
            adapter = LocalExecutor(runner, Resolver(), replacement=Replacement())
            try:
                self.assertEqual(await adapter.execute(work, gate), ('succeeded','identity-applied'))
                self.assertEqual((await runner.run(('git','-C',str(root/'repo'),'config','user.email'))).strip(),
                                 'fixture@example.test')
                restart = Work.parse({**work.wire(), 'action':'restart_session'})
                self.assertEqual(await adapter.execute(restart, gate), ('unknown','effect-running'))
                ready = Work.parse({**work.wire(), 'action':'confirm_ready'})
                with self.assertRaises(Unavailable):
                    await adapter.execute(ready, gate)
                await adapter.aclose()
                cleanup = ProcessRunner()
                try:
                    # Потеря control runtime не убивает уже живую pane/session.
                    alive = await cleanup.run(('tmux','-S',str(socket),'display-message','-p',
                                               '-t',pane,'#{pane_dead}'))
                    self.assertEqual(alive.strip(), '0')
                finally:
                    await cleanup.aclose()
            finally:
                cleanup = ProcessRunner()
                await cleanup.run(('tmux','-S',str(socket),'kill-server'))
                await cleanup.aclose()
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

    async def test_revocation_between_identity_writes_neither_sends_nor_rolls_back(self):
        work = Work.parse(wire())
        root = Path('/tmp/mop640-fake-binding')
        target = BoundTarget(work.target_id, work.incarnation_id, work.node_id,
            work.executor_epoch, work.generation, work.role, work.bundle_digest,
            work.grant_id, root, root/'tmux', '%0', root/'launcher')
        effects = []
        checks = 0
        class Resolver:
            async def resolve(self, value): return target, ActionInput('Owner','owner@example.test','message')
        class Runner:
            async def run(self, argv, *, before_spawn, final_check):
                await before_spawn()
                final_check()
                effects.append(argv)
                return ''
            async def aclose(self): pass
        async def authorize():
            nonlocal checks
            checks += 1
            if checks == 2: raise Refused('generation changed')
        class Sender:
            async def send(self, *args): raise AssertionError('revoked work sent a message')
            async def aclose(self): pass
        adapter = LocalExecutor(Runner(), Resolver(), sender=Sender())
        with self.assertRaisesRegex(Refused, 'generation changed'):
            await adapter.execute(work, EffectGuard(authorize, lambda: None))
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0][-2:], ('user.name','Owner'))
        await adapter.aclose()

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
                    kwargs['final_check']()
                    calls.append(tuple(argv))
                    if argv[-2] == 'var':
                        return 'Owner <owner@example.test> 1 +0000\n'
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
