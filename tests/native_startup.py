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
from _native_startup_fixture import Client, EPOCH, Executor, NODE, PREVIOUS, state
from mop.node.http_execution import MachineClient, Refused, Unavailable

TOKEN = 'b' * 40
EXISTING_ACTIONS = frozenset(('restart_session', 'apply_identity', 'send_message',
                              'confirm_ready', 'wipe_clone'))


def existing_surface_bindings(api, executor):
    """Положительный helper прежних effects, не включение новых physical ports."""
    from mop.node import http_execution
    with patch.object(http_execution, 'ACTIONS', EXISTING_ACTIONS):
        return api.NativeBindings(executor)


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


# BUG: повторный cancel после construction теряет join runtime cleanup.
# HYPOTHESIS: shield сохраняет задачу, но caller возвращается раньше неё;
# asyncio.run отменяет оставшегося owner при закрытии loop.
# SOLUTION: startup удерживает один aclose task и join через все caller cancel;
# собственный deadline Runtime остаётся неизменным и его отказ не маскируется.
# RESULT: loop shutdown больше не отменяет cleanup; stuck port даёт Unavailable.
# STATUS: FIXED — see #736
class RuntimeCancellationTests(unittest.TestCase):
    def test_repeated_cancel_joins_runtime_cleanup_before_loop_shutdown(self):
        from mop.node import native_startup as api
        client = Client()
        events = []
        runtimes = []

        async def scenario():
            reconcile_entered, cleanup_entered, release = (asyncio.Event() for _ in range(3))

            class ControlledExecutor(Executor):
                async def reconcile(self, *args):
                    self.calls.append(('reconcile', *args))
                    reconcile_entered.set()
                    await asyncio.Event().wait()

                async def aclose(self):
                    events.append('cleanup-entered')
                    cleanup_entered.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        events.append('cleanup-cancelled')
                        raise
                    events.append('cleanup-completed')
                    self.closed = True

            class CapturedRuntime(api.NativeRuntime):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    runtimes.append(self)

            executor = ControlledExecutor(client.calls)
            with patch.object(api, 'NativeRuntime', CapturedRuntime):
                startup = asyncio.create_task(api.start(client, executor,
                    installation_id='fixture', node_id=NODE))
                await asyncio.wait_for(reconcile_entered.wait(), 1)
                startup.cancel()
                await asyncio.wait_for(cleanup_entered.wait(), 1)
                # Каждый cancel доставляется во время cleanup, не до construction.
                for _ in range(3):
                    startup.cancel()
                    for _ in range(5):
                        await asyncio.sleep(0)
                events.append('startup-held' if not startup.done() else 'startup-escaped')
                if not startup.done():
                    release.set()
                try:
                    await startup
                except asyncio.CancelledError:
                    events.append('startup-cancelled')
                events.append('scenario-returned')

        asyncio.run(scenario())
        # Проверяем именно результат loop shutdown, а не существование shield.
        self.assertEqual(events, ['cleanup-entered', 'startup-held', 'cleanup-completed',
                                  'startup-cancelled', 'scenario-returned'])
        self.assertTrue(client.closed)
        self.assertEqual([row[0] for row in client.calls], ['discover', 'reconcile'])
        self.assertEqual(len(runtimes), 1)
        self.assertTrue(runtimes[0]._close_task.done())
        self.assertFalse(runtimes[0]._close_task.cancelled())
        self.assertTrue(runtimes[0]._cleanup_task.done())
        self.assertFalse(runtimes[0]._cleanup_task.cancelled())

    def test_repeated_cancel_stuck_runtime_cleanup_reports_bounded_failure(self):
        from mop.node import native_startup as api
        events, failures = [], []
        client = Client()

        async def scenario():
            reconcile_entered, cleanup_entered, release = (asyncio.Event() for _ in range(3))

            class StuckExecutor(Executor):
                async def reconcile(self, *args):
                    self.calls.append(('reconcile', *args))
                    reconcile_entered.set()
                    await asyncio.Event().wait()

                async def aclose(self):
                    events.append('cleanup-entered')
                    cleanup_entered.set()
                    await release.wait()
                    events.append('cleanup-completed')

            executor = StuckExecutor(client.calls)
            startup = asyncio.create_task(api.start(client, executor,
                installation_id='fixture', node_id=NODE))
            await asyncio.wait_for(reconcile_entered.wait(), 1)
            startup.cancel()
            await asyncio.wait_for(cleanup_entered.wait(), 1)
            before = time.monotonic()
            async def cancel_repeatedly():
                while not startup.done():
                    startup.cancel()
                    await asyncio.sleep(.01)
            canceller = asyncio.create_task(cancel_repeatedly())
            try:
                await asyncio.wait_for(startup, 10.5)
            except BaseException as error:
                failures.append(error)
            finally:
                await canceller
                elapsed = time.monotonic() - before
                release.set()
                # Только test teardown дожидается зависшего port после отказа;
                # production не вправе выдавать его за physical quiescence.
                remaining = asyncio.all_tasks() - {asyncio.current_task()}
                await asyncio.gather(*remaining, return_exceptions=True)
            return elapsed

        elapsed = asyncio.run(scenario())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], Unavailable, repr(failures[0]))
        self.assertIn('shutdown incomplete', str(failures[0]))
        self.assertGreaterEqual(elapsed, 4.8)
        self.assertLess(elapsed, 10.5)
        self.assertEqual(api.STARTUP_TIMEOUT, 5.0)
        self.assertEqual(events, ['cleanup-entered', 'cleanup-completed'])
        self.assertTrue(client.closed)
        self.assertEqual([row[0] for row in client.calls], ['discover', 'reconcile'])


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
            # TLS callback, не собственный check DSL (#675).
            async def discovery_scenario():
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
            asyncio.run(discovery_scenario())
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
                async def refused_discovery_scenario():
                    client = api.machine_client(bootstrap)
                    try:
                        with self.assertRaises((Refused, Unavailable)) as caught:
                            await api.check(client, installation_id='fixture', node_id=NODE)
                        self.assertNotIn(TOKEN, str(caught.exception))
                    finally:
                        await client.aclose()
                asyncio.run(refused_discovery_scenario())
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
