#!/usr/bin/env python3
"""HYPOTHESIS: запуск читает файловые markers вместо addressed assignment.
SOLUTION: immutable registration/layout, exact API response и physical/session ports.
RESULT: воспроизведены отсутствие addressed boundary/readiness/producer/probe,
Unix pathname overflow (121 > 100), bool/schema и control characters, затем
11 checks зелёные. Настоящий owned
process/Unix socket, stdlib hook IPC и настоящий verified HTTPS receipt
(включая 401 чужого credential) проверены. Server auth/receipt policy/image/routes —
контролируемые doubles, production SessionApi/PVE не подтверждены.
STATUS: FIXED — see #643 (driver/node stage, Root binding отдельно)
"""
import asyncio
import base64
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import UUID, uuid4

import _lib  # noqa: F401 -- общий временный корень процесса (#338)
import hermetic  # noqa: F401
from _body_fixture import api, work, bundle, wire
from mop.node.http_execution import Refused, Unavailable
from mop.node.execution_adapters import BoundTarget, ActionInput



class LaunchTests(unittest.TestCase):
    def test_machine_configuration_keeps_https_lan_advertised_host_and_ssh_port_separate(self):
        m = api()
        config = m.MachineConfig('https://control.example.test:8443', '10.20.0.4',
                                 '192.0.2.23', 2207, 'host')
        self.assertEqual(config.control_origin, 'https://control.example.test:8443')
        self.assertEqual(config.routing_reference, '10.20.0.4')
        self.assertEqual(config.bootstrap_address, '192.0.2.23:2207')
        for fields in ({'control_origin':'http://control.test'}, {'routing_reference':''},
                       {'bootstrap_host':''}, {'ssh_port':True}, {'ssh_port':65536},
                       {'driver':'docker'}, {'bootstrap_host':'host;touch /tmp/x'},
                       {'control_origin':'https://token@control.test'},
                       {'control_origin':'https://control.test\n'}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                replace(config, **fields)

    def test_registered_layout_never_takes_display_paths_or_commands_from_work(self):
        m = api()
        w = work()
        registration = m.Registration(w, str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        layout = m.RegisteredLayout(Path('/owned'), registration)
        self.assertEqual(layout.clone, Path('/owned/puppets/pu-owned-1'))
        self.assertEqual(layout.launcher, Path('/owned/runtime')/w.incarnation_id/'launch')
        self.assertIn(w.incarnation_id, str(layout.tmux_socket))
        for name in ('../../other', 'pu-other-1; rm', '', 'not-a-puppet', 'pu-owned-1\n'):
            with self.assertRaises(ValueError):
                replace(registration, display_name=name)
        with self.assertRaises(ValueError): m.RegisteredLayout(Path('relative'), registration)

    def test_layout_fits_unix_socket_limit_without_truncating_identity(self):
        m = api()
        w = work()
        r = m.Registration(w, str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        layout = m.RegisteredLayout(Path('/tmp/mop-643-owned-session'), r)
        self.assertLessEqual(len(os.fsencode(layout.runtime/'session.sock')), 100)
        other = m.RegisteredLayout(layout.home, replace(r, work=replace(w, incarnation_id=str(uuid4()))))
        self.assertNotEqual(layout.runtime, other.runtime)

    def test_infeasible_unix_socket_path_is_refused_before_any_spawn(self):
        # BUG (#677): layout, чей session.sock длиннее sockaddr_un (108 с NUL),
        # принимается как доверенный: ребёнок умирает на bind молча, до «ready»,
        # и контракт живого процесса не проверяется вовсе.
        # HYPOTHESIS: RegisteredLayout не знает границы sockaddr_un, поэтому
        # полный hermetic прогон (вложенные корни процессов, ~113-120 байт)
        # тихо превращает проверку живого процесса в проверку трупа.
        # SOLUTION: RegisteredLayout отказывает с явной причиной до spawn.
        # RESULT: 49-байтовый home (107 байт session.sock) принимается,
        # 50-байтовый (108) отказывает; bind на настоящем kernel подтверждает
        # границу: 107 проходит, 108 -- AF_UNIX path too long.
        m = api()
        w = work()
        r = m.Registration(w, str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        feasible = m.RegisteredLayout(Path('/tmp/'+'h'*44), r)
        self.assertEqual(len(os.fsencode(feasible.runtime/'session.sock')), 107)
        # 108-байтовый session.sock -- тот же layout по арифметике production
        # (home/'runtime'/incarnation/'session.sock'); после отказа конструктор
        # сам такой объект уже не строит, длину считает проверка.
        infeasible_home = '/tmp/'+'h'*45
        infeasible_sock = f'{infeasible_home}/runtime/{w.incarnation_id}/session.sock'
        self.assertEqual(len(os.fsencode(infeasible_sock)), 108)
        with self.assertRaisesRegex(ValueError, 'sockaddr_un'):
            m.RegisteredLayout(Path(infeasible_home), r)

    def test_exact_work_bootstrap_image_and_approved_bytes_are_required(self):
        m = api()
        b = bundle()
        w = replace(work(), bundle_digest=b.digest)
        r = m.Registration(w, str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        raw = dict(schema_version=1, work=w.wire(), bootstrap_revision=r.bootstrap_revision,
            bootstrap_digest=r.bootstrap_digest, image_digest=r.image_digest,
            manifest=b.manifest.to_dict(), prompt=base64.b64encode(b.prompt).decode(),
            git_name='Current Owner', git_email='owner@example.test', message='', peer_token=None)
        response = m.LaunchResponse.parse(raw, r, input_factory=ActionInput)
        self.assertEqual(response.bundle.prompt, b.prompt)
        self.assertEqual(response.inputs, ActionInput('Current Owner', 'owner@example.test'))
        for change in ({'work':{**w.wire(), 'schema_version':True}},
                       {'work':replace(w, generation=9).wire()},
                       {'work':replace(w, executor_epoch=str(uuid4())).wire()},
                       {'work':replace(w, grant_id=str(uuid4())).wire()},
                       {'bootstrap_revision':str(uuid4())}, {'bootstrap_digest':'d'*64},
                       {'image_digest':'e'*64}, {'prompt':base64.b64encode(b'changed').decode()},
                       {'operator_token':'forbidden'}, {'argv':['sh']}, {'clone':'/other'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                m.LaunchResponse.parse({**raw, **change}, r, input_factory=ActionInput)


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolver_uses_current_addressed_input_and_live_scope_not_old_markers(self):
        m = api()
        from mop.node.body_launch import BindingsResolver, SessionProof
        b = bundle()
        w = replace(work(), bundle_digest=b.digest)
        r = m.Registration(w, str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        # Короткий собственный корень (#677): во вложенном прогоне наследуемый
        # TMPDIR удлиняет путь за границу sockaddr_un, и RegisteredLayout
        # отказывает заранее; сокет здесь двойник, но путь обязан оставаться
        # осуществимым, как у настоящего layout.
        with tempfile.TemporaryDirectory(prefix='m643-', dir='/tmp') as directory:
            root = Path(directory)
            layout = m.RegisteredLayout(root, r)
            root.joinpath('bus.json').write_text('old')
            root.joinpath('ready.json').write_text('old')
            root.joinpath('identity.json').write_text('old')
            response = m.LaunchResponse(r, b, ActionInput('Owner', 'owner@example.test'))
            class Source:
                async def fetch(self, requested):
                    self.requested = requested
                    return response
            class Physical:
                async def observe(self, registered):
                    return SessionProof(registered.registration, '%0', 'session-new',
                        UUID(w.executor_epoch), root/'session.sock', 123, 'start-1')
                async def reconcile(self, node, previous, new): return False
            source, physical = Source(), Physical()
            resolver = BindingsResolver(layout, source=source, physical=physical)
            target, inputs = await resolver.resolve(w)
            self.assertIs(type(target), BoundTarget)
            target.check(w)
            self.assertEqual(target.clone, layout.clone)
            self.assertEqual(inputs, response.inputs)
            self.assertEqual(source.requested, w)
            self.assertFalse(await resolver.reconcile(w.node_id, str(uuid4()), w.executor_epoch))
            with self.assertRaises(Refused):
                await resolver.resolve(replace(w, generation=8))
            physical.observe = lambda registered: asyncio.sleep(0, result=None)
            with self.assertRaises(Unavailable): await resolver.resolve(w)

    async def test_addressed_source_parses_raw_current_api_bytes_without_paths_or_authority_claims(self):
        m = api()
        from mop.node import body_launch
        self.assertTrue(hasattr(body_launch, 'AddressedLaunchSource'), 'addressed API source is missing')
        b = bundle()
        w = replace(work(), bundle_digest=b.digest)
        r = m.Registration(w, str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        value = dict(schema_version=1, work=w.wire(), bootstrap_revision=r.bootstrap_revision,
            bootstrap_digest=r.bootstrap_digest, image_digest=r.image_digest,
            manifest=b.manifest.to_dict(), prompt=base64.b64encode(b.prompt).decode(),
            git_name='Current Owner', git_email='owner@example.test', message='', peer_token=None)
        class Query:
            async def read_launch(self, requested):
                self.requested = requested
                return value
        query = Query()
        source = body_launch.AddressedLaunchSource(r, query=query)
        response = await source.fetch(w)
        self.assertEqual(response.bundle.prompt, b.prompt)
        self.assertEqual(query.requested, w.wire())
        value['work'] = replace(w, owner_id=5).wire()
        with self.assertRaises(Refused): await source.fetch(w)
        unbound = body_launch.AddressedLaunchSource(r)
        with self.assertRaisesRegex(Unavailable, 'API'):
            await unbound.fetch(w)

    async def test_unbound_production_session_capability_refuses_without_effect(self):
        api()
        from mop.node.body_launch import UnboundSessionApi
        with self.assertRaisesRegex(Unavailable, 'scope'):
            await UnboundSessionApi().observe(None)

    async def test_bootstrap_orders_external_image_route_admission_and_always_revokes(self):
        m = api()
        from mop.node.body_launch import BootstrapDiscovery
        w = work()
        registration = m.Registration(w, str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        for driver in ('host', 'pve'):
            for fails in ('', 'image', 'route', 'bootstrap'):
                calls = []
                config = m.MachineConfig('https://public.example.test', '10.20.0.4',
                    '192.0.2.8' if driver == 'host' else '10.77.1.8', 2207, driver)
                class Ports:
                    supports_bootstrap = True
                    async def image(self, node_id, digest):
                        calls.append(('image', node_id, digest))
                        return fails != 'image'
                    async def route(self, machine):
                        calls.append(('route', machine.routing_reference, machine.bootstrap_address))
                        return fails != 'route'
                    async def admit(self, registered, allow): calls.append(('admit', allow))
                    async def bootstrap(self, registered, machine):
                        calls.append(('bootstrap', registered.bootstrap_revision))
                        if fails == 'bootstrap': raise Unavailable('bootstrap failed')
                        return registered.bootstrap_revision
                unsupported = Ports()
                unsupported.supports_bootstrap = False
                with self.assertRaisesRegex(Unavailable, 'capabilit'):
                    await BootstrapDiscovery(config, registration, unsupported).run()
                self.assertEqual(calls, [])
                discovery = BootstrapDiscovery(config, registration, Ports())
                if fails:
                    with self.assertRaises(Unavailable): await discovery.run()
                else:
                    self.assertEqual(await discovery.run(), registration.bootstrap_revision)
                names = [row[0] for row in calls]
                self.assertEqual(names[:1], ['image'])
                if fails == 'image': self.assertEqual(names, ['image'])
                elif fails == 'route': self.assertEqual(names, ['image', 'route'])
                else:
                    self.assertEqual(names, ['image', 'route', 'admit', 'bootstrap', 'admit'])
                    self.assertEqual(calls[-1], ('admit', False))


class BootstrapCancellationTests(unittest.IsolatedAsyncioTestCase):
    # BUG: повторная отмена caller убивает обязательный отзыв admission.
    # HYPOTHESIS: unshielded await после первой отмены передаёт вторую _revoke.
    # HYPOTHESIS 2 (WRONG): Task.cancelling() различает текущую отмену;
    # проверка показала ложный success после ранее обработанной отмены.
    # SOLUTION: отмена только текущего run запускает независимо owned отзыв;
    # aclose ограничивает ожидание, не отменяет cleanup и сохраняет причину отказа.
    # RESULT: fixture-файл удалён после 1/2/5 отмен; owned cleanup завершается
    # без orphan tasks; timeout/ошибка/отмена cleanup не подтверждают отзыв.
    # STATUS: FIXED — see #643
    async def test_repeated_caller_cancellation_does_not_abandon_admission(self):
        from mop.node.body_launch import BootstrapDiscovery
        m = api()
        r = m.Registration(work(), str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        machine = m.MachineConfig('https://example.test', '127.0.0.1', '127.0.0.1', 2207, 'host')
        for cancellations in (1, 2, 5):
            with self.subTest(cancellations=cancellations), tempfile.TemporaryDirectory(prefix='m643-revoke-') as directory:
                admission = Path(directory)/'temporary-admission'
                started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
                baseline = asyncio.all_tasks()
                class Ports:
                    supports_bootstrap = True
                    async def image(self, node_id, digest): return True
                    async def route(self, machine): return True
                    async def bootstrap(self, registration, machine): return registration.bootstrap_revision
                    async def admit(self, registration, allow):
                        if allow:
                            admission.write_text('owned nonsecret admission fixture')
                        else:
                            started.set()
                            try:
                                await release.wait()
                                admission.unlink()
                            finally:
                                finished.set()
                discovery = BootstrapDiscovery(machine, r, Ports())
                caller = asyncio.create_task(discovery.run())
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    for _ in range(cancellations):
                        caller.cancel()
                        await asyncio.sleep(0)
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await caller
                    await asyncio.wait_for(finished.wait(), 2)
                    await asyncio.sleep(0)
                    self.assertFalse(admission.exists(), 'repeated cancellation abandoned temporary admission')
                    pending = asyncio.all_tasks() - baseline
                    self.assertFalse(pending, f'orphaned cleanup tasks: {pending}')
                finally:
                    release.set()
                    await asyncio.gather(caller, return_exceptions=True)

    async def test_cancelled_caller_keeps_cleanup_owned_and_close_is_bounded_and_cancellable(self):
        from mop.node.body_launch import BootstrapDiscovery
        m = api()
        r = m.Registration(work(), str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        machine = m.MachineConfig('https://example.test', '127.0.0.1', '127.0.0.1', 2207, 'host')
        with tempfile.TemporaryDirectory(prefix='m643-revoke-') as directory:
            admission = Path(directory)/'temporary-admission'
            started, release = asyncio.Event(), asyncio.Event()
            baseline = asyncio.all_tasks()
            class Ports:
                supports_bootstrap = True
                async def image(self, node_id, digest): return True
                async def route(self, machine): return True
                async def bootstrap(self, registration, machine): return registration.bootstrap_revision
                async def admit(self, registration, allow):
                    if allow:
                        admission.write_text('owned nonsecret admission fixture')
                    else:
                        started.set()
                        await release.wait()
                        admission.unlink()
            discovery = BootstrapDiscovery(machine, r, Ports())
            caller = asyncio.create_task(discovery.run())
            closer = None
            try:
                await asyncio.wait_for(started.wait(), 2)
                caller.cancel()
                done, _ = await asyncio.wait({caller}, timeout=.1)
                self.assertIn(caller, done, 'caller cancellation must not wait for physical revocation')
                with self.assertRaises(asyncio.CancelledError): await caller
                self.assertTrue(admission.exists())
                cleanup = asyncio.all_tasks() - baseline
                self.assertEqual(len(cleanup), 1, 'exactly one cleanup owner must survive caller cancellation')
                with self.assertRaisesRegex(Unavailable, 'not confirmed'):
                    await asyncio.wait_for(discovery.aclose(timeout=.01), .5)
                self.assertTrue(admission.exists(), 'bounded wait is not physical revocation')
                self.assertTrue(all(not task.done() for task in cleanup))
                for _ in range(2):
                    closer = asyncio.create_task(discovery.aclose(timeout=1))
                    await asyncio.sleep(0)
                    closer.cancel()
                    with self.assertRaises(asyncio.CancelledError): await closer
                self.assertTrue(all(not task.done() for task in cleanup), 'close cancellation killed owned cleanup')
                release.set()
                await discovery.aclose(timeout=1)
                self.assertFalse(admission.exists())
                self.assertTrue(all(task.done() for task in cleanup))
                self.assertFalse(asyncio.all_tasks() - baseline, 'cleanup left orphan tasks')
                with self.assertRaises(Unavailable): await discovery.run()
            finally:
                release.set()
                await asyncio.gather(caller, *([] if closer is None else [closer]), return_exceptions=True)
                await asyncio.gather(*(asyncio.all_tasks() - baseline), return_exceptions=True)

    async def test_handled_earlier_cancellation_cannot_skip_revocation_confirmation(self):
        from mop.node.body_launch import BootstrapDiscovery
        m = api()
        r = m.Registration(work(), str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        machine = m.MachineConfig('https://example.test', '127.0.0.1', '127.0.0.1', 2207, 'host')
        started, release = asyncio.Event(), asyncio.Event()
        class Ports:
            supports_bootstrap = True
            async def image(self, node_id, digest): return True
            async def route(self, machine): return True
            async def bootstrap(self, registration, machine): return registration.bootstrap_revision
            async def admit(self, registration, allow):
                if not allow:
                    started.set()
                    await release.wait()
        discovery = BootstrapDiscovery(machine, r, Ports())
        async def previously_cancelled():
            asyncio.current_task().cancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                pass
            return await discovery.run()
        caller = asyncio.create_task(previously_cancelled())
        try:
            await asyncio.wait_for(started.wait(), 2)
            self.assertFalse(caller.done(), 'historical cancellation count is not revocation confirmation')
        finally:
            release.set()
            await caller
            await discovery.aclose(timeout=1)

    async def test_close_during_bootstrap_revokes_and_failed_revocation_is_not_confirmation(self):
        from mop.node.body_launch import BootstrapDiscovery
        m = api()
        r = m.Registration(work(), str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        machine = m.MachineConfig('https://example.test', '127.0.0.1', '127.0.0.1', 2207, 'host')
        for failure in (None, Unavailable('fixture revocation refused'), TimeoutError(), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory(prefix='m643-revoke-') as directory:
                admission = Path(directory)/'temporary-admission'
                started = asyncio.Event()
                class Ports:
                    supports_bootstrap = True
                    async def image(self, node_id, digest): return True
                    async def route(self, machine): return True
                    async def bootstrap(self, registration, machine):
                        started.set()
                        await asyncio.Event().wait()
                    async def admit(self, registration, allow):
                        if allow:
                            admission.write_text('owned nonsecret admission fixture')
                        elif failure is not None:
                            raise failure
                        else:
                            admission.unlink()
                discovery = BootstrapDiscovery(machine, r, Ports())
                caller = asyncio.create_task(discovery.run())
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    if failure is None:
                        await asyncio.wait_for(discovery.aclose(timeout=1), 2)
                        self.assertFalse(admission.exists())
                    else:
                        with self.assertRaisesRegex(Unavailable, 'not confirmed') as caught:
                            await asyncio.wait_for(discovery.aclose(timeout=1), 2)
                        if isinstance(failure, Unavailable):
                            self.assertIn(str(failure), str(caught.exception))
                        self.assertTrue(admission.exists())
                        with self.assertRaisesRegex(Unavailable, 'not confirmed'):
                            await discovery.aclose(timeout=1)
                finally:
                    caller.cancel()
                    await asyncio.gather(caller, return_exceptions=True)


class PhysicalProcessTests(unittest.IsolatedAsyncioTestCase):
    async def verify_https_receipt(self, proof):
        # Контролируемый HTTPS receiver проверяет transport и адресный ACK,
        # не подменяет server auth/ORM или production SessionApi scope authority.
        import ssl
        import subprocess
        from aiohttp import web
        from mop.node.http_execution import Admission, MachineClient, HttpsTransport
        w = proof.registration.work
        attempt = str(uuid4())
        credential = 'owned-fixture-machine-credential'
        received = []
        async def result(request):
            if request.headers.get('Authorization') != 'Token '+credential:
                return web.json_response({}, status=401)
            value = await request.json()
            if value.get('attempt_id') != attempt:
                return web.json_response({}, status=409)
            received.append((proof.scope, value))
            return web.json_response(dict(schema_version=1, receipt_id=value['receipt_id'],
                attempt_id=attempt, revision='2', outcome=value['outcome'], evidence=value['evidence']))
        with tempfile.TemporaryDirectory(prefix='m643-tls-') as directory:
            key, cert = Path(directory)/'key.pem', Path(directory)/'cert.pem'
            completed = await asyncio.to_thread(subprocess.run, ['openssl', 'req', '-x509',
                '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=localhost',
                '-addext', 'subjectAltName=IP:127.0.0.1', '-keyout', str(key), '-out', str(cert)],
                capture_output=True, timeout=10)
            self.assertEqual(completed.returncode, 0, 'owned TLS fixture certificate generation failed')
            key.chmod(0o600)
            server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_tls.load_cert_chain(cert, key)
            client_tls = ssl.create_default_context(cafile=str(cert))
            app = web.Application()
            app.router.add_post('/api/v1/machine/work/result', result)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, '127.0.0.1', 0, ssl_context=server_tls)
            await site.start()
            origin = 'https://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])
            client = MachineClient(HttpsTransport(origin, credential, tls=client_tls))
            denied = MachineClient(HttpsTransport(origin, 'wrong-fixture-credential', tls=client_tls))
            admission = Admission(w, str(uuid4()), attempt, 1, w.not_after)
            try:
                with self.assertRaises(Refused):
                    await denied.result(admission, 'unknown', 'effect-running')
                await client.result(admission, 'unknown', 'effect-running')
                self.assertEqual(len(received), 1)
                self.assertEqual(received[0][0], proof.scope)
                self.assertEqual(received[0][1]['outcome'], 'unknown')
                self.assertEqual(received[0][1]['evidence'], 'effect-running')
            finally:
                await client.aclose()
                await denied.aclose()
                await runner.cleanup()

    async def test_live_owned_process_socket_and_scope_are_required_not_a_stale_session_file(self):
        m = api()
        from mop.node import body_launch
        self.assertTrue(hasattr(body_launch, 'SessionApiProbe'), 'physical SessionApi probe is missing')
        from mop.node.body_launch import SessionApiProbe, SessionProof, process_identity
        w = work()
        r = m.Registration(w, str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        # HYPOTHESIS (#677): полный hermetic прогон вкладывает корни процессов
        # друг в друга (TMPDIR родителя наследуется), и session.sock на
        # /tmp/mop-test-run.../mop-test-run.../m643.../runtime/UUID/session.sock
        # разрастается до ~120 байт > 107 sockaddr_un: ребёнок умирает на bind
        # до «ready», и контракт живого процесса не проверяется вовсе.
        # SOLUTION (#677): короткий собственный корень фикстуры прямо в /tmp,
        # не наследующий длину родительских каталогов; владение и удаление --
        # самой проверкой, не общим корнем процесса.
        with tempfile.TemporaryDirectory(prefix='m643-', dir='/tmp') as directory:
            layout = m.RegisteredLayout(Path(directory), r)
            layout.clone.mkdir(parents=True)
            layout.runtime.mkdir(parents=True, mode=0o700)
            socket_path = layout.runtime/'session.sock'
            script = ('import asyncio,sys\n'
                      'async def receive(reader,writer):\n'
                      ' writer.close(); await writer.wait_closed()\n'
                      'async def main():\n'
                      ' server=await asyncio.start_unix_server(receive,path=sys.argv[1])\n'
                      ' print("ready",flush=True)\n'
                      ' async with server: await server.serve_forever()\n'
                      'asyncio.run(main())\n')
            process = await asyncio.create_subprocess_exec(sys.executable, '-I', '-u', '-c', script,
                str(socket_path), cwd=layout.clone, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True)
            try:
                self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 5), b'ready\n')
                proof = SessionProof(r, '%0', 'fixture-session', uuid4(), socket_path,
                    process.pid, process_identity(process.pid))
                class Api:
                    supports_launch_scope = True
                    async def observe(self, registered): return proof
                probe = SessionApiProbe(Api())
                self.assertEqual(await probe.observe(layout), proof)
                await self.verify_https_receipt(proof)
                proof = replace(proof, process_start='old-file-start')
                with self.assertRaises(Unavailable): await probe.observe(layout)
                proof = replace(proof, process_start=process_identity(process.pid))
                process.terminate()
                await process.wait()
                self.assertTrue(socket_path.exists())
                with self.assertRaises(Unavailable): await probe.observe(layout)
                with self.assertRaisesRegex(Unavailable, 'scope'):
                    SessionApiProbe(object())
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()


class SessionConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_requires_exact_receipt_model_and_unchanged_live_process(self):
        m = api()
        from mop.node import body_launch
        self.assertTrue(hasattr(body_launch, 'ReadyConfirmation'), 'receipt-bound readiness is missing')
        from mop.node.body_launch import BindingsResolver, ReadyConfirmation, ReadyReceipt, SessionProof
        w = work()
        r = m.Registration(w, str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        layout = m.RegisteredLayout(Path('/owned'), r)
        proof = SessionProof(r, '%0', 'session-new', uuid4(), Path('/owned/session.sock'), 123, 'start-1')
        class Physical:
            async def observe(self, layout): return proof
        resolver = BindingsResolver(layout, source=None, physical=Physical())
        target = BoundTarget(w.target_id, w.incarnation_id, w.node_id, w.executor_epoch,
            w.generation, w.role, w.bundle_digest, w.grant_id, layout.clone,
            layout.tmux_socket, '%0', layout.launcher, proof.session_socket, proof)
        receipt = ReadyReceipt(r, proof.scope, proof.pid, proof.process_start, '42', 'confirmed-model')
        class Receipts:
            desired_model = 'confirmed-model'
            async def ready(self, requested, observed): return receipt
        confirmation = ReadyConfirmation(resolver, Receipts())
        self.assertTrue(await confirmation.confirmed(w, target))
        for bad in (replace(receipt, model=None), replace(receipt, process_start='old-start'),
                    replace(receipt, scope=replace(proof.scope, producer_epoch=uuid4()))):
            receipt = bad
            self.assertFalse(await confirmation.confirmed(w, target))
        receipt = ReadyReceipt(r, proof.scope, proof.pid, proof.process_start, '42', 'confirmed-model')
        class RacingReceipts:
            desired_model = 'confirmed-model'
            async def ready(self, requested, observed):
                nonlocal proof
                proof = replace(proof, process_start='replacement-during-HTTP')
                return receipt
        self.assertFalse(await ReadyConfirmation(resolver, RacingReceipts()).confirmed(w, target))

    async def test_producer_cannot_attach_without_preexec_preparation(self):
        # Полный env/ACK/IPC happy path теперь в session_runtime.py: настоящий
        # owned child вместо proof со synthetic PID=123 и env подменой parent.
        from mop.node.body_launch import SessionRuntime
        runtime = SessionRuntime(None, None)
        try:
            with self.assertRaises(Unavailable):
                runtime.environment()
            with self.assertRaises(Unavailable):
                await runtime.attach()
        finally:
            await runtime.aclose()


class SessionIdentityTests(unittest.IsolatedAsyncioTestCase):
    # BUG: BoundTarget теряет immutable proof, а sender доверяет pathname.
    # HYPOTHESIS: rebind принимает новый разговор/процесс по тем же путям;
    # после connect проверяется новое имя сокета, а не уже подключённый peer.
    # SOLUTION: перенос полного frozen SessionProof в BoundTarget, exact readiness
    # и SO_PEERCRED/start/cwd/namespace gate открытого writer перед любыми bytes;
    # native user frame несёт session_id, а drain остаётся unknown.
    # RESULT: исходный RED -- 7 assertion failures (4 identity substitutions,
    # readiness, missing session_id, connected peer). Owned kernel peers зелёные;
    # intentional restart передаёт старый proof в replacement port, не требует
    # прежней identity после замены. Native Claude/пул не проверялись.
    # STATUS: FIXED — see #710
    async def asyncSetUp(self):
        from mop.node.body_launch import BindingsResolver, SessionProof, process_identity
        m = api()
        self.directory = tempfile.TemporaryDirectory(prefix='m710-', dir='/tmp')
        self.addCleanup(self.directory.cleanup)
        b = bundle()
        self.work = replace(work(), bundle_digest=b.digest)
        r = m.Registration(self.work, str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        self.layout = m.RegisteredLayout(Path(self.directory.name), r)
        self.layout.clone.mkdir(parents=True)
        self.layout.runtime.mkdir(parents=True)
        self.path = self.layout.runtime/'session.sock'
        self.child = await self.listener(self.path)
        self.proof = SessionProof(r, '%0', 'owned-conversation', uuid4(), self.path,
                                  self.child.pid, process_identity(self.child.pid))
        self.inputs = ActionInput('Owner', 'owner@example.test', 'owned message', 'owned-token')
        response = m.LaunchResponse(r, b, self.inputs)
        class Source:
            async def fetch(inner, requested): return response
        class Physical:
            async def observe(inner, layout): return self.proof
        self.resolver = BindingsResolver(self.layout, source=Source(), physical=Physical())

    async def listener(self, path):
        # Настоящий kernel peer; только собственные процессы и сокеты, ни registry,
        # ни key-файлов пользователя. Это не native Claude/SessionApi acceptance.
        script = ('import asyncio,base64,sys\n'
                  'async def receive(reader,writer):\n'
                  ' data=await reader.read()\n'
                  ' print("closed:"+base64.b64encode(data).decode(),flush=True)\n'
                  ' writer.close(); await writer.wait_closed()\n'
                  'async def main():\n'
                  ' server=await asyncio.start_unix_server(receive,path=sys.argv[1])\n'
                  ' print("ready",flush=True)\n'
                  ' async with server: await server.serve_forever()\n'
                  'asyncio.run(main())\n')
        child = await asyncio.create_subprocess_exec(sys.executable, '-I', '-u', '-c', script,
            str(path), cwd=self.layout.clone, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        async def cleanup():
            if child.returncode is None: child.kill()
            await child.wait()
        self.addAsyncCleanup(cleanup)
        self.assertEqual(await asyncio.wait_for(child.stdout.readline(), 5), b'ready\n')
        return child

    async def received(self, child):
        line = await asyncio.wait_for(child.stdout.readline(), 2)
        self.assertTrue(line.startswith(b'closed:'), line)
        return base64.b64decode(line[len(b'closed:'):])

    async def test_rebind_rejects_same_path_session_producer_process_and_start_substitution(self):
        from mop.node.execution_adapters import LocalExecutor
        from mop.node.http_execution import EffectGuard
        from mop.node.body_launch import process_identity
        original = self.proof
        variants = (('conversation', replace(original, session_id='after-clear')),
                    ('producer', replace(original, producer_epoch=uuid4())),
                    ('start', replace(original, process_start='different-boot-or-start')),
                    ('process', None))
        for kind, changed in variants:
            with self.subTest(identity=kind):
                self.proof = original
                effects = []
                class Runner:
                    async def run(inner, argv, *, before_spawn, final_check):
                        await before_spawn()
                        final_check()
                        effects.append(argv)
                        return 'Owner <owner@example.test> 1 +0000\n' if 'var' in argv else ''
                    async def aclose(inner): pass
                async def authorize():
                    nonlocal changed
                    if kind == 'process' and changed is None:
                        self.child.terminate()
                        await self.child.wait()
                        self.path.unlink()
                        newer = await self.listener(self.path)
                        changed = replace(original, pid=newer.pid, process_start=process_identity(newer.pid))
                    self.proof = changed
                executor = LocalExecutor(Runner(), self.resolver)
                try:
                    with self.assertRaisesRegex(Refused, 'binding'):
                        await executor.execute(self.work, EffectGuard(authorize, lambda: None))
                    self.assertEqual(effects, [], 'substituted session received a physical effect')
                finally:
                    await executor.aclose()

    async def test_restart_confirms_deliberate_old_to_new_identity_not_old_identity_again(self):
        from mop.node.execution_adapters import LocalExecutor
        from mop.node.http_execution import EffectGuard
        from mop.node.body_launch import process_identity
        self.work = replace(self.work, action='restart_session')
        r = replace(self.proof.registration, work=self.work)
        self.proof = replace(self.proof, registration=r)
        self.layout = replace(self.layout, registration=r)
        self.resolver.layout = self.layout
        response = api().LaunchResponse(r, bundle(), self.inputs)
        class Source:
            async def fetch(inner, work): return response
        self.resolver.source = Source()
        original = self.proof
        replacement = await self.listener(self.layout.runtime/'replacement.sock')
        newer = replace(original, session_id='replacement', producer_epoch=uuid4(),
                        pid=replacement.pid, process_start=process_identity(replacement.pid))
        calls = []
        class Runner:
            async def run(inner, argv, *, before_spawn, final_check):
                await before_spawn()
                final_check()
                self.assertIn('respawn-pane', argv)
                calls.append('respawn')
                self.child.terminate()
                await self.child.wait()
                self.proof = newer
                return ''
            async def aclose(inner): pass
        class Replacement:
            async def confirmed(inner, work, target):
                self.assertEqual(target.session_proof, original)
                self.assertEqual(self.proof, newer)
                self.assertIsNotNone(self.child.returncode)
                calls.append('confirmed')
                return True
        async def authorize(): pass
        executor = LocalExecutor(Runner(), self.resolver, replacement=Replacement())
        try:
            self.assertEqual(await executor.execute(self.work, EffectGuard(authorize, lambda: None)),
                             ('succeeded', 'session-replaced'))
            self.assertEqual(calls, ['respawn', 'confirmed'])
        finally:
            await executor.aclose()

    async def test_ready_receipt_for_new_conversation_cannot_confirm_old_bound_target(self):
        from mop.node.body_launch import ReadyConfirmation, ReadyReceipt
        w = self.work
        target, _ = await self.resolver.resolve(w)
        self.proof = replace(self.proof, session_id='after-clear')
        calls = []
        class Receipts:
            async def ready(inner, requested, proof):
                calls.append(proof)
                return ReadyReceipt(proof.registration, proof.scope, proof.pid,
                                    proof.process_start, str(uuid4()), 'owned-model')
        with self.assertRaisesRegex(Refused, 'readiness'):
            await ReadyConfirmation(self.resolver, Receipts()).confirmed(w, target)
        self.assertEqual(calls, [], 'new conversation was queried for old readiness')

    async def test_native_user_frame_pins_conversation_but_drain_stays_unknown(self):
        from mop.node.execution_adapters import UnixSessionSender
        target, inputs = await self.resolver.resolve(self.work)
        sender = UnixSessionSender()
        async def gate(): pass
        try:
            self.assertEqual(await sender.send(target, inputs, gate), ('unknown', 'effect-running'))
            auth, frame = [json.loads(line) for line in (await self.received(self.child)).splitlines()]
            self.assertEqual(auth, {'type':'auth', 'token':'owned-token'})
            self.assertEqual(frame.get('session_id'), self.proof.session_id,
                             'native user frame lost the documented conversation fence')
        finally:
            await sender.aclose()

    async def test_connected_peer_is_checked_even_when_path_rebind_matches_original_target(self):
        from mop.node.execution_adapters import UnixSessionSender
        import socket
        import struct
        target, inputs = await self.resolver.resolve(self.work)
        parked = self.layout.runtime/'parked.sock'
        self.path.rename(parked)
        foreign = await self.listener(self.path)
        async def gate():
            # Соединение sender уже с чужим listener; новое чтение pathname видит
            # старый правильный peer. Проверка пути или повторный connect не gate.
            parked.replace(self.path)
            current, _ = await self.resolver.resolve(self.work)
            self.assertEqual(current, target)
            _, writer = await asyncio.open_unix_connection(str(self.path))
            try:
                peer = writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                self.assertEqual(struct.unpack('3i', peer)[0], self.child.pid)
            finally:
                writer.close()
                await writer.wait_closed()
        sender = UnixSessionSender()
        try:
            with self.assertRaisesRegex(Unavailable, 'peer'):
                await sender.send(target, inputs, gate)
            self.assertEqual(await self.received(foreign), b'', 'foreign peer received auth/message bytes')
        finally:
            await sender.aclose()


if __name__ == '__main__': unittest.main()
