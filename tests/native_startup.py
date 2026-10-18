"""Startup boundary #640; unit order плюс настоящий bounded HTTPS client.

HYPOTHESIS: старый Runtime.start сначала меняет server fence, затем сверяет OS,
а executor_state без installation coordinates позволяет принять чужой controller.
SPEC: discovery -> installation/node validation -> physical reconcile -> restart
CAS -> poll; ни CLI --check, ни unbound/failed root не имеют права двигать fence.
SOLUTION: отдельный bounded startup owner и полный explicit physical root.
RESULT: RED — composition отсутствовал; отдельные RED нашли cleanup при отказе
client construction и unbounded direct start. Unit order/cleanup checks зелёные;
own TLS доказывает real MachineClient/auth/CA/bounds и отказ неполного discovery,
не реальную reconciliation. Полный live wire связан под #639/#640, OS root не связан.
STATUS: FIXED — see #640; native effectful acceptance здесь не заявляется.
"""
import asyncio
import importlib.util
import http.server
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import _lib  # noqa: F401
import hermetic  # noqa: F401
from _control_fixture import own_tls
from mop.node.http_execution import MachineClient, Refused, Unavailable

NODE, EPOCH, PREVIOUS = (str(uuid4()) for _ in range(3))
TOKEN = 'b' * 40
EXISTING_ACTIONS = frozenset(('restart_session', 'apply_identity', 'send_message',
                              'confirm_ready', 'wipe_clone'))


def existing_surface_bindings(api, executor):
    """Положительный helper прежних effects, не включение новых physical ports."""
    from mop.node import http_execution
    with patch.object(http_execution, 'ACTIONS', EXISTING_ACTIONS):
        return api.NativeBindings(executor)


def state(**change):
    return dict(schema_version=1, installation_id='fixture', epoch=EPOCH,
                node_id=NODE, executor_epoch=PREVIOUS, **change)


class Client:
    """Unit port; TLS checks ниже не подменяют MachineClient/transport."""
    def __init__(self, value=None):
        self.value = state() if value is None else value
        self.calls = []
        self.closed = False
        self.fail_restart = False

    async def executor_state(self, node):
        self.calls.append(('discover', node))
        return self.value

    async def restarted(self, node, previous, new):
        self.calls.append(('restart', node, previous, new))
        if self.fail_restart:
            raise Unavailable('restart reply lost')

    async def poll(self, node, executor, limit):
        self.calls.append(('poll', node, executor, limit))
        return ()

    async def aclose(self):
        self.closed = True


class Executor:
    def __init__(self, calls):
        self.calls = calls
        self.quiet = True
        self.closed = False
        self.block = False

    async def reconcile(self, node, previous, new):
        self.calls.append(('reconcile', node, previous, new))
        if self.block:
            await asyncio.Event().wait()
        return self.quiet

    async def execute(self, *args):
        raise AssertionError('no effect in a startup test')

    async def aclose(self):
        self.closed = True


class StartupTests(unittest.IsolatedAsyncioTestCase):
    def api(self):
        name = 'mop.node.native_startup'
        self.assertIsNotNone(importlib.util.find_spec(name), 'safe native startup composition is missing')
        return __import__(name, fromlist=['native_startup'])

    async def test_discovery_precedes_physical_reconcile_before_restart_and_poll(self):
        api = self.api()
        client = Client()
        executor = Executor(client.calls)
        runtime = await api.start(client, executor, installation_id='fixture', node_id=NODE)
        self.addAsyncCleanup(runtime.aclose)
        await runtime.tick()
        self.assertEqual([row[0] for row in client.calls], ['discover', 'reconcile', 'restart', 'poll'])
        self.assertEqual(runtime.epoch, EPOCH)
        self.assertNotEqual(runtime.executor_epoch, PREVIOUS)
        self.assertEqual(client.calls[1][1:], client.calls[2][1:])
        self.assertEqual(client.calls[1][2], PREVIOUS)
        self.assertEqual(runtime.executor_epoch, client.calls[1][3])

    async def test_no_restart_poll_on_wrong_installation_node_epoch_or_old_discovery(self):
        api = self.api()
        for value in (PREVIOUS, {**state(), 'installation_id': 'foreign'},
                {**state(), 'node_id': str(uuid4())}, {**state(), 'epoch': ''},
                {**state(), 'epoch': '00000000-0000-0000-0000-000000000000'},
                {**state(), 'executor_epoch': True}, {**state(), 'actions': []}):
            client = Client(value)
            executor = Executor(client.calls)
            with self.subTest(value=value), self.assertRaises((Refused, Unavailable)):
                await api.start(client, executor, installation_id='fixture', node_id=NODE)
            self.assertEqual([row[0] for row in client.calls], ['discover'])
            self.assertTrue(client.closed)
            self.assertTrue(executor.closed)

    async def test_uncertain_reconciliation_never_advances_fence(self):
        api = self.api()
        for result in (False, None, 1):
            client = Client()
            executor = Executor(client.calls)
            executor.quiet = result
            with self.assertRaises(Unavailable):
                await api.start(client, executor, installation_id='fixture', node_id=NODE)
            self.assertEqual([row[0] for row in client.calls], ['discover', 'reconcile'])
            self.assertTrue(client.closed)
            self.assertTrue(executor.closed)

    async def test_reconciliation_deadline_is_at_most_five_seconds_no_fence(self):
        api = self.api()
        client = Client()
        executor = Executor(client.calls)
        executor.block = True
        before = time.monotonic()
        with self.assertRaises(Unavailable):
            await asyncio.wait_for(api.start(client, executor, installation_id='fixture', node_id=NODE), 5.5)
        self.assertLess(time.monotonic()-before, 5.5)
        self.assertEqual([row[0] for row in client.calls], ['discover', 'reconcile'])
        self.assertTrue(client.closed)
        self.assertTrue(executor.closed)

    async def test_native_runtime_start_port_itself_preserves_five_second_bound(self):
        api = self.api()
        client = Client()
        executor = Executor(client.calls)
        executor.block = True
        runtime = api.NativeRuntime(client, executor, node_id=NODE, epoch=EPOCH,
                                    executor_epoch=str(uuid4()))
        self.addAsyncCleanup(runtime.aclose)
        with self.assertRaises(Unavailable):
            await asyncio.wait_for(runtime.start(PREVIOUS), 5.5)
        self.assertEqual([row[0] for row in client.calls], ['reconcile'])
        with self.assertRaises(Unavailable):
            await runtime.tick()

    async def test_lost_restart_never_polls_or_retries_and_cleanup_is_owned(self):
        api = self.api()
        client = Client()
        client.fail_restart = True
        executor = Executor(client.calls)
        with self.assertRaisesRegex(Unavailable, 'reply lost'):
            await api.start(client, executor, installation_id='fixture', node_id=NODE)
        self.assertEqual([row[0] for row in client.calls], ['discover', 'reconcile', 'restart'])
        self.assertTrue(client.closed)
        self.assertTrue(executor.closed)

    async def test_cancel_during_reconcile_never_restarts_and_closes_owned_clients(self):
        api = self.api()
        client = Client()
        executor = Executor(client.calls)
        executor.block = True
        task = asyncio.create_task(api.start(client, executor, installation_id='fixture', node_id=NODE))
        for _ in range(20):
            if len(client.calls) == 2:
                break
            await asyncio.sleep(.001)
        self.assertEqual([row[0] for row in client.calls], ['discover', 'reconcile'])
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(client.closed)
        self.assertTrue(executor.closed)
        self.assertEqual([row[0] for row in client.calls], ['discover', 'reconcile'])

    async def test_complete_bindings_refuse_unknown_capabilities_and_cleanup_client_construction_failure(self):
        api = self.api()
        from mop.node.execution_adapters import LocalExecutor, ProcessRunner
        class Complete:
            async def resolve(self, *args): pass
            async def reconcile(self, *args): raise Unavailable('no physical scope')
            async def confirmed(self, *args): pass
            async def quiescent(self, *args): pass
            async def wipe(self, *args): pass
        complete = Complete()
        executor = LocalExecutor(ProcessRunner(), complete, readiness=complete,
            replacement=complete, body=complete)
        self.addAsyncCleanup(executor.aclose)
        bindings = existing_surface_bindings(api, executor)
        from mop.node import http_execution
        # #721 расширил wire, но physical implementations здесь не появились.
        self.assertGreater(http_execution.ACTIONS, EXISTING_ACTIONS)
        with self.assertRaisesRegex(Unavailable, 'capabilities'):
            api.NativeBindings(executor)
        with patch.object(http_execution, 'ACTIONS', http_execution.ACTIONS | {'create_body'}):
            with self.assertRaisesRegex(Unavailable, 'capabilities'):
                api.NativeBindings(executor)
        with patch.object(api, 'machine_client', side_effect=Refused('invalid CA')):
            with self.assertRaisesRegex(Refused, 'invalid CA'):
                await api.serve(object(), bindings=bindings)
        self.assertTrue(executor.runner._closed)
        self.assertTrue(executor.sender._closed)

    async def test_missing_or_incomplete_production_bindings_refuse_before_effects(self):
        api = self.api()
        from mop.node.execution_adapters import LocalExecutor, ProcessRunner
        executor = LocalExecutor(ProcessRunner(), None)
        try:
            with self.assertRaisesRegex(Unavailable, 'physical'):
                existing_surface_bindings(api, executor)
        finally:
            await executor.aclose()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.calls.append((self.command, self.path, self.headers.get('Authorization')))
        self.send_response(self.server.status)
        self.send_header('Content-Type', 'application/json')
        raw = self.server.raw
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        self.server.calls.append((self.command, self.path, self.headers.get('Authorization')))
        self.send_error(500, 'unexpected startup mutation')


class RealTLSStartup(unittest.TestCase):
    def api(self):
        name = 'mop.node.native_startup'
        self.assertIsNotNone(importlib.util.find_spec(name), 'safe native startup composition is missing')
        return __import__(name, fromlist=['native_startup'])

    def test_real_bounded_machine_discovery_old_contract_is_not_installation_authority(self):
        api = self.api()
        from mop.node.bootstrap_transport import load
        with own_tls(Handler) as (server, origin, ca, directory):
            ca.chmod(0o600)
            file = Path(directory)/'bootstrap.json'
            file.write_text(json.dumps(dict(schema_version=1, installation_id='fixture',
                origin=origin, ca_file=str(ca), node_id=NODE, token=TOKEN)))
            file.chmod(0o600)
            server.calls, server.status = [], 200
            server.raw = json.dumps(dict(schema_version=1, node_id=NODE, executor_epoch=PREVIOUS)).encode()
            bootstrap = load(file)
            async def check():
                client = api.machine_client(bootstrap)
                self.assertIs(type(client), MachineClient)
                self.assertLessEqual(client.transport.timeout, 5)
                self.assertLessEqual(client.transport.response_limit, 262144)
                try:
                    with self.assertRaisesRegex(Refused, 'schema'):
                        await api.check(client, installation_id='fixture', node_id=NODE)
                finally:
                    await client.aclose()
                self.assertTrue(client.transport._closed)
                self.assertTrue(client.transport._session.closed)
            asyncio.run(check())
            self.assertEqual(server.calls, [('GET', '/api/v1/machine/executor?schema_version=1&node_id='+NODE,
                                            'Token '+TOKEN)])
            import subprocess
            root = Path(__file__).resolve().parents[1]
            result = subprocess.run([str(root/'bin/mop'), 'agent', '--bootstrap', str(file), '--check'],
                capture_output=True, text=True, env=os.environ.copy(), timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn('schema', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertNotIn(TOKEN, result.stderr)
            self.assertEqual([row[0] for row in server.calls], ['GET', 'GET'])

    def test_real_tls_refuses_wrong_ca_auth_redirect_and_oversized_response(self):
        api = self.api()
        from mop.node.bootstrap_transport import load, BootstrapError
        with own_tls(Handler) as (server, origin, ca, directory):
            ca.chmod(0o600)
            file = Path(directory)/'bootstrap.json'
            doc = dict(schema_version=1, installation_id='fixture', origin=origin,
                       ca_file=str(ca), node_id=NODE, token=TOKEN)
            file.write_text(json.dumps(doc))
            file.chmod(0o600)
            bootstrap = load(file)
            for status, raw in ((401, b'{}'), (302, b'{}'), (200, b' ' * 262145)):
                server.calls, server.status, server.raw = [], status, raw
                async def check():
                    client = api.machine_client(bootstrap)
                    try:
                        with self.assertRaises((Refused, Unavailable)) as caught:
                            await api.check(client, installation_id='fixture', node_id=NODE)
                        self.assertNotIn(TOKEN, str(caught.exception))
                    finally:
                        await client.aclose()
                asyncio.run(check())
                self.assertEqual(len(server.calls), 1)
                self.assertEqual(server.calls[0][0], 'GET')
            # Другой собственный CA: TLS handshake отказывает до HTTP/auth bytes.
            with own_tls(Handler) as (_, _, other_ca, _):
                other_ca.chmod(0o600)
                file.write_text(json.dumps({**doc, 'ca_file': str(other_ca)}))
                server.calls = []
                async def check_ca():
                    client = api.machine_client(load(file))
                    try:
                        with self.assertRaises(Unavailable):
                            await api.check(client, installation_id='fixture', node_id=NODE)
                    finally:
                        await client.aclose()
                asyncio.run(check_ca())
                self.assertEqual(server.calls, [])
            ca.write_text('not a certificate')
            file.write_text(json.dumps(doc))
            with self.assertRaisesRegex(BootstrapError, 'CA is invalid'):
                api.machine_client(load(file))


if __name__ == '__main__':
    unittest.main()
