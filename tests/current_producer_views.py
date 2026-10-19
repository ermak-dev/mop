#!/usr/bin/env python3
"""HYPOTHESIS: lifetime registration не совпадает с Work следующей фазы.
SPEC: только authenticated exact admission+launch разрешают root view; immutable
pins/paths/native identity прежние, producer ACK/epoch единственные. Observe
не создаёт view, закрытый source не оживает, ready/effects не выдумываются.
SOLUTION: явный узловой current view с finite authenticated proof port.
RESULT: 9 тестов RED на отсутствии capability; отдельно RED на post-await
expiry и воскрешении после последнего snapshot. Все 13 GREEN. HTTPS настоящий,
admission/launch policy и native snapshot здесь — контролируемые doubles;
существующие реальные native/ACK/bootstrap checks проверяются отдельно.
STATUS: FIXED — see #643 (node view, не production composition).

#733: doubles обновлены на finite initial-launch readiness schema без role bytes.
Actual server/TLS/PG17 purpose contract проверяет _current_view_purpose_cases;
post-launch identity теперь unbound, readiness effect guard по-прежнему обязателен.

#792: измерено на owned PG17/TLS fixture. proof() до фикса: 9 HTTPS-запросов
(claim + initial-launch + claim трижды, 4.98s) и 4.997s из production 5s;
snapshot'ы — 0.0009s. После фикса: 3 claim recheck, 0.63s. Причина — повторный
полный approval fetch на каждое подтверждение producer epoch. Фикс не кеширует
authority и не снимает ни один claim/fence: recheck проходит тот же server
claim с restore fence, assignment/cas и revoke-able launch grant.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
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
from _body_fixture import bundle, work
from mop.driver.launch import RegisteredLayout, Registration
from mop.node.body_launch import BindingsResolver, ReadyConfirmation, ReadyReceipt, SessionRuntime
from mop.node.execution_adapters import ActionInput
from mop.node.http_execution import Admission, HttpsTransport, MachineClient, Refused, Unavailable
from mop.node.native_observer import NativeIdentity, NativePhysicalSessions


class CurrentViewsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import web
        self.directory = tempfile.TemporaryDirectory(prefix='m643v-', dir='/tmp')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.root.chmod(0o700)
        self.bundle = bundle()
        self.initial = replace(work(), action='native_exec', bundle_digest=self.bundle.digest)
        self.image_revision = str(uuid4())
        self.image_policy = dict(policy_id=str(uuid4()), cpu=100, memory_mb=256, disk_mb=1024)
        image_digest = hashlib.sha256(('image-revision:'+self.image_revision).encode()).hexdigest()
        self.registration = Registration(self.initial, str(uuid4()), 'b'*64, image_digest, 'pu-owned-1')
        self.layout = RegisteredLayout(self.root, self.registration)
        self.layout.runtime.mkdir(parents=True, mode=0o700)
        self.identity = NativeIdentity('%0', 123, 'boot:start', 'owned-session', self.root/'config',
                                       (1, 2), (3, 4), (5, 6), (7, 8))
        self.snapshot = patch('mop.node.native_observer._snapshot', side_effect=lambda layout: self.identity)
        self.snapshot.start()
        self.addCleanup(self.snapshot.stop)
        self.registered, self.rows, self.calls = [], [], []
        self.ack_started, self.ack_release = asyncio.Event(), asyncio.Event()
        self.ack_release.set()
        test = self
        class RegistrationSource:
            async def register(inner, proof):
                test.registered.append(proof)
                test.ack_started.set()
                await test.ack_release.wait()
                return proof.scope
            async def ingest(inner, row):
                test.rows.append(row)
                raise AssertionError('no hooks should be fabricated')
        source = RegistrationSource()
        self.runtime = SessionRuntime(source, source)
        self.addAsyncCleanup(self.runtime.aclose)
        self.runtime.prepare(self.layout, path=self.layout.runtime/'hook.sock')
        self.current = replace(self.initial, step_id=str(uuid4()), action='confirm_ready',
                               revision=self.initial.revision+2,
                               not_after=self.initial.not_after+timedelta(seconds=1))
        self.current_registration = replace(self.registration, work=self.current)
        self.server_work = self.current
        self.attempt, self.request = str(uuid4()), str(uuid4())
        self.lease = datetime.now(timezone.utc)+timedelta(seconds=40)
        self.revoked, self.pause_path, self.pin_change = False, None, {}
        self.http_entered, self.http_release = asyncio.Event(), asyncio.Event()
        self.http_release.set()
        token = 'owned-current-view-fixture'
        async def route(request):
            if request.headers.get('Authorization') != 'Token '+token:
                return web.json_response({}, status=401)
            value = await request.json()
            path = request.path.rsplit('/', 1)[-1]
            self.calls.append(path)
            if self.pause_path == path:
                self.http_entered.set()
                await self.http_release.wait()
            if self.revoked:
                return web.json_response({}, status=403)
            if path == 'claim':
                if value['work'] != self.server_work.wire() or value['request_id'] != self.request:
                    return web.json_response({}, status=409)
                return web.json_response(dict(schema_version=1, attempt_id=self.attempt,
                    revision='1', state='claimed', lease_until=self.lease.isoformat(),
                    work=self.server_work.wire()))
            if value != self.server_work.wire():
                return web.json_response({}, status=409)
            return web.json_response(dict(schema_version=1, work=self.server_work.wire(),
                phase=self.server_work.action, bootstrap_revision=self.registration.bootstrap_revision,
                bootstrap_digest=self.registration.bootstrap_digest,
                image_digest=self.registration.image_digest, display_name=self.registration.display_name,
                image_revision=self.image_revision, image_policy=self.image_policy, selection=dict(policy_id=self.request,
                    digest='d'*64, source=dict(origin='https://example.test/owned.git',
                    commit='e'*40, ref='refs/heads/owned'), desired_model='fixture-model',
                    model_config_digest='f'*64, consumers=[dict(purpose='model_config', version_id=self.attempt)]),
                role=None, bootstrap=None, consumers=[],
                git_name='Owner', git_email='owner@example.test') | self.pin_change)
        key, cert = self.root/'key.pem', self.root/'cert.pem'
        completed = await asyncio.to_thread(subprocess.run, ['openssl', 'req', '-x509',
            '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=localhost',
            '-addext', 'subjectAltName=IP:127.0.0.1', '-keyout', str(key), '-out', str(cert)],
            capture_output=True, timeout=10)
        self.assertEqual(completed.returncode, 0, 'owned TLS fixture generation failed')
        key.chmod(0o600)
        server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_tls.load_cert_chain(cert, key)
        client_tls = ssl.create_default_context(cafile=str(cert))
        app = web.Application()
        app.router.add_post('/api/v1/machine/work/claim', route)
        app.router.add_post('/api/v1/machine/work/initial-launch', route)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, '127.0.0.1', 0, ssl_context=server_tls)
        await site.start()
        origin = 'https://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])
        self.client = MachineClient(HttpsTransport(origin, token, tls=client_tls))
        self.addAsyncCleanup(self.client.aclose)
        self.admission = Admission(self.current, self.request, self.attempt, 1, self.lease)

    def authority(self, registration=None, admission=None, client=None):
        self.assertIsNotNone(importlib.util.find_spec('mop.node.producer_views'),
                             'explicit authenticated current Work attachment view is missing')
        from mop.node.producer_views import CurrentLaunchAuthority
        return CurrentLaunchAuthority(client or self.client, registration or self.current_registration,
                                      admission=admission or self.admission)

    async def activate(self):
        await self.runtime.attach()
        self.assertEqual(len(self.registered), 1)

    async def view(self, **kwargs):
        authority = self.authority(**kwargs)
        self.assertTrue(callable(getattr(self.runtime, 'current_view', None)),
                        'root cannot bind a current Work view to the existing producer')
        return await self.runtime.current_view(authority)

    async def test_two_phase_works_share_one_producer_but_proofs_match_only_current_registration(self):
        await self.activate()
        original = self.registered[0]
        self.assertFalse(self.registration.matches(self.current))
        foreign_layout = replace(self.layout, registration=self.current_registration)
        with self.assertRaises(Unavailable):
            await NativePhysicalSessions(owner=self.runtime).observe(foreign_layout)
        view = await self.view()
        self.assertIsInstance(view, BindingsResolver)
        proof = await view.proof(self.current)
        self.assertEqual(proof.registration, self.current_registration)
        self.assertTrue(proof.registration.matches(self.current))
        self.assertFalse(proof.registration.matches(self.initial))
        self.assertEqual(replace(proof, registration=self.registration), original)
        self.assertEqual(proof.scope, original.scope)
        self.assertEqual((view.layout.clone, view.layout.runtime, view.layout.tmux_socket, view.layout.launcher),
                         (self.layout.clone, self.layout.runtime, self.layout.tmux_socket, self.layout.launcher))
        target, inputs = await view.resolve(self.current)
        target.check(self.current)
        self.assertEqual(target.session_proof, proof)
        self.assertEqual(inputs, ActionInput('Owner', 'owner@example.test'))
        self.assertEqual(await NativePhysicalSessions(owner=self.runtime).observe(self.layout), original)
        self.assertIn('initial-launch', self.calls)
        self.assertIn('claim', self.calls)
        self.assertEqual(len(self.registered), 1, 'per-view producer registration is forbidden')
        self.assertEqual(self.rows, [], 'views must not fabricate hook readiness')
        with self.assertRaises(Refused):
            await view.proof(self.initial)

    async def test_epoch_confirmation_rechecks_the_claim_without_reissuing_approved_bytes(self):
        # HYPOTHESIS (#792): proof() проходит полный approval fetch (claim +
        # initial-launch + claim) на каждое подтверждение producer epoch.
        # На медленной машине три таких круга не влезают в production 5s,
        # хотя физические snapshots стоят доли миллисекунды.
        # SOLUTION: подтверждение epoch делает только current-claim recheck,
        # не перевыдавая уже одобренные initial-launch bytes.
        # RESULT: RED — proof() вызывает initial-launch трижды.
        await self.activate()
        view = await self.view()
        self.calls.clear()
        proof = await view.proof(self.current)
        self.assertEqual(proof.registration, self.current_registration)
        self.assertNotIn('initial-launch', self.calls,
                         'epoch confirmation must not reissue approved initial-launch bytes')
        self.assertGreaterEqual(self.calls.count('claim'), 1,
                                'epoch confirmation must recheck the current admission')

    async def test_every_immutable_binding_is_exact_before_any_current_api_call(self):
        await self.activate()
        for name in ('epoch', 'operation_id', 'target_id', 'incarnation_id', 'node_id',
                     'executor_epoch', 'grant_id', 'owner_id', 'generation', 'role', 'bundle_digest'):
            old = getattr(self.current, name)
            changed = old+1 if type(old) is int else 'other' if name == 'role' else 'd'*64 if name == 'bundle_digest' else str(uuid4())
            w = replace(self.current, **{name: changed})
            self.calls.clear()
            with self.subTest(field=name), self.assertRaises(Refused):
                await self.view(registration=replace(self.current_registration, work=w),
                                admission=replace(self.admission, work=w))
            self.assertEqual(self.calls, [])
        for name, changed in (('bootstrap_revision', str(uuid4())), ('bootstrap_digest', 'd'*64),
                              ('image_digest', 'd'*64), ('display_name', 'pu-other-1')):
            self.calls.clear()
            with self.subTest(field=name), self.assertRaises(Refused):
                await self.view(registration=replace(self.current_registration, **{name: changed}))
            self.assertEqual(self.calls, [])
        self.assertEqual(len(self.registered), 1)

    async def test_json_or_untrusted_transport_is_not_authenticated_current_evidence(self):
        await self.activate()
        authority = self.authority()
        with self.assertRaises(Refused):
            await self.runtime.current_view(self.current.wire())
        class CallerJson:
            async def request(inner, *args, **kwargs):
                raise AssertionError('arbitrary caller JSON is not a root API port')
        with self.assertRaises(Refused):
            self.authority(client=MachineClient(CallerJson()))
        with self.assertRaises((AttributeError, Refused)):
            authority.client = MachineClient(CallerJson())
        transport = self.client.transport
        self.client.transport = CallerJson()
        try:
            with self.assertRaises(Refused):
                await self.runtime.current_view(authority)
        finally:
            self.client.transport = transport

    async def test_revoked_expired_changed_admission_and_unapproved_launch_fail_closed(self):
        await self.activate()
        authority = self.authority()
        self.revoked = True
        with self.assertRaises(Refused):
            await self.runtime.current_view(authority)
        self.revoked = False
        for fields in ({'attempt_id': str(uuid4())}, {'revision': 2},
                       {'lease_until': datetime.now(timezone.utc)-timedelta(seconds=1)}):
            with self.subTest(fields=fields), self.assertRaises(Refused):
                await self.view(admission=replace(self.admission, **fields))
        for pin in ('bootstrap_revision', 'bootstrap_digest', 'image_digest'):
            # Сервер вернул другие approved pins: даже valid HTTPS не разрешает их принять.
            self.pin_change = {pin: str(uuid4()) if pin == 'bootstrap_revision' else 'f'*64}
            with self.subTest(pin=pin), self.assertRaises(Refused):
                await self.runtime.current_view(authority)
        self.pin_change = {}
        expired = replace(self.current, not_after=datetime.now(timezone.utc)-timedelta(seconds=1))
        with self.assertRaises(Refused):
            await self.view(registration=replace(self.current_registration, work=expired),
                            admission=replace(self.admission, work=expired))
        self.assertEqual(self.rows, [])

    # HYPOTHESIS (#738): valid HTTPS metadata может подменить canonical label,
    # пока current view сверяет только bootstrap/image pins своей registration.
    # SOLUTION: exact name относится к тому же immutable physical layout.
    # RESULT: RED Refused not raised; 19 current-view checks и PG125 + UID4 GREEN.
    # STATUS: FIXED — see #738.
    async def test_canonical_name_must_match_attached_registration(self):
        await self.activate()
        self.pin_change = {'display_name': 'pu-other-1'}
        with self.assertRaises(Refused):
            await self.view()
        self.assertEqual(len(self.registered), 1)
        self.assertEqual(self.rows, [])

    async def test_pending_ack_and_prepared_runtime_never_expose_a_current_view(self):
        authority = self.authority()
        with self.assertRaises(Unavailable):
            await self.runtime.current_view(authority)
        self.ack_release.clear()
        task = asyncio.create_task(self.runtime.attach())
        try:
            await asyncio.wait_for(self.ack_started.wait(), 2)
            self.calls.clear()
            with self.assertRaises(Unavailable):
                await self.runtime.current_view(authority)
            self.assertEqual(self.calls, [])
            self.assertFalse((self.layout.runtime/'hook.sock').exists())
        finally:
            self.ack_release.set()
            await task
        self.assertEqual(self.rows, [])

    async def test_revoke_after_view_creation_is_rechecked_not_cached_authority(self):
        await self.activate()
        view = await self.view()
        self.revoked = True
        for method in (view.proof, view.resolve):
            with self.assertRaises(Refused):
                await method(self.current)
        with self.assertRaises(Refused):
            await NativePhysicalSessions(owner=view).observe(view.layout)
        self.assertEqual(self.rows, [])
        self.assertEqual(len(self.registered), 1)

    async def test_close_during_launch_api_await_cannot_resurrect_source(self):
        await self.activate()
        authority = self.authority()
        self.pause_path = 'initial-launch'
        self.http_release.clear()
        task = asyncio.create_task(self.runtime.current_view(authority))
        try:
            await asyncio.wait_for(self.http_entered.wait(), 2)
            await self.runtime.aclose()
            self.http_release.set()
            with self.assertRaises(Unavailable):
                await task
            with self.assertRaises(Unavailable):
                await self.view()
        finally:
            self.http_release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(self.registered), 1)
        self.assertEqual(self.rows, [])

    async def test_session_change_permanently_invalidates_source(self):
        await self.activate()
        view = await self.view()
        old = self.identity
        self.identity = replace(old, session_id='after-clear')
        with self.assertRaises(Unavailable):
            await view.proof(self.current)
        self.identity = old
        with self.assertRaises(Unavailable):
            await view.proof(self.current)
        with self.assertRaises(Unavailable):
            await self.view()
        self.assertEqual(self.rows, [])

    async def test_expiry_during_final_observer_await_cannot_return_current_proof(self):
        await self.activate()
        view = await self.view()
        # Изолируем последний observer await, не HTTP duration: post-await
        # deadline gate обязателен даже после последнего физического snapshot.
        original_observe = view.physical.observe
        async def expired_after_native(layout):
            proof = await original_observe(layout)
            self.lease = datetime.now(timezone.utc)-timedelta(seconds=1)
            return proof
        # Последнее await самого observer должно быть закрыто local gate view.proof.
        with patch.object(view.physical, 'observe', side_effect=expired_after_native):
            from mop.node import producer_views
            now = datetime.now(timezone.utc)
            class Clock:
                @staticmethod
                def now(tz):
                    return now if self.lease > now else now+timedelta(minutes=5)
            with patch.object(producer_views, 'datetime', Clock), self.assertRaises(Refused):
                await view.proof(self.current)

    async def test_process_change_during_launch_api_await_permanently_closes_lifetime(self):
        await self.activate()
        view = await self.view()
        old = self.identity
        self.pause_path = 'initial-launch'
        self.http_release.clear()
        task = asyncio.create_task(view.resolve(self.current))
        try:
            await asyncio.wait_for(self.http_entered.wait(), 2)
            self.identity = replace(old, pid=456, process_start='boot:new-start')
            self.http_release.set()
            with self.assertRaises(Unavailable):
                await task
            self.identity = old
            with self.assertRaises(Unavailable):
                await self.view()
            with self.assertRaises(Unavailable):
                await NativePhysicalSessions(owner=self.runtime).observe(self.layout)
        finally:
            self.http_release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(self.registered), 1)
        self.assertEqual(self.rows, [])

    async def test_no_view_can_resurrect_after_last_native_snapshot_detects_path_replacement(self):
        await self.activate()
        view = await self.view()
        # Шесть anchor snapshots на API proof не заменяют самый последний
        # current-layout snapshot. Он видит иной inode, затем старый возвращён.
        old = self.identity
        seen = 0
        def snapshot(layout):
            nonlocal seen
            if layout.registration == self.current_registration:
                seen += 1
                if seen == 3:
                    return replace(old, socket_inode=(9, 10))
            return old
        with patch('mop.node.native_observer._snapshot', side_effect=snapshot):
            with self.assertRaises(Unavailable):
                await NativePhysicalSessions(owner=view).observe(view.layout)
        self.assertEqual(seen, 3)
        with self.assertRaises(Unavailable):
            await self.view()

    async def test_current_view_does_not_replace_machine_effect_guard(self):
        from mop.node.execution_adapters import LocalExecutor
        from mop.node.http_execution import EffectGuard
        await self.activate()
        view = await self.view()
        # Успешный readiness view не заменяет независимый machine effect guard.
        # Post-launch identity честно неподдержан: native-only bytes не fallback.
        identity_work = replace(self.current, action='apply_identity', step_id=str(uuid4()),
                                revision=self.current.revision+1)
        self.calls.clear()
        with self.assertRaisesRegex(Unavailable, 'identity.*purpose'):
            await self.view(registration=replace(self.current_registration, work=identity_work),
                            admission=replace(self.admission, work=identity_work))
        self.assertEqual(self.calls, [])
        effects = []
        class Runner:
            async def run(inner, argv, *, before_spawn, final_check):
                await before_spawn()
                final_check()
                effects.append(argv)
                raise AssertionError('refused admission must precede every effect')
            async def aclose(inner): pass
        async def refused():
            raise Refused('fixture machine admission revoked before effect')
        class Readiness:
            async def confirmed(inner, work, target):
                raise AssertionError('machine guard must precede readiness effect')
        executor = LocalExecutor(Runner(), view, readiness=Readiness())
        try:
            with self.assertRaisesRegex(Refused, 'admission revoked'):
                await executor.execute(self.current, EffectGuard(refused, lambda: None))
        finally:
            await executor.aclose()
        self.assertEqual(effects, [])
        self.assertEqual(len(self.registered), 1)
        self.assertEqual(self.rows, [])

    # HYPOTHESIS #734: отменённый to_thread теряет позднюю physical ошибку,
    # а последний успешный snapshot не перепроверяет закрывшегося owner.
    # Проверки ниже используют настоящий tmux/proc/registry/socket и реальный
    # worker thread; HTTPS policy fixture по-прежнему controlled, не native CLI.
    # SOLUTION: синхронная source invalidation на timeout/cancel плюс lookup
    # владельца после последнего snapshot; никакого нового producer epoch/ACK.
    # RESULT: 5/5 RED (old proof/active source), затем весь view suite 18/18 GREEN;
    # ещё 7 native/runtime/binding/client/layer suites GREEN, budget прежний 5s.
    # STATUS: FIXED — see #734; native CLI/model/retirement не подтверждены.
    async def native_process(self):
        if not shutil.which('tmux'):
            hermetic.skip('current view cancellation', 'tmux is required')
            self.skipTest('tmux is required')
        # Убираем snapshot double только для этих физических race-проверок.
        self.snapshot.stop()
        self.layout.clone.mkdir(parents=True)
        config = self.root/'native-config'
        config.mkdir()
        base = ['tmux', '-S', str(self.layout.tmux_socket)]
        environment = dict(HOME=str(self.root), CLAUDE_CONFIG_DIR=str(config), PATH=os.defpath,
            TERM='xterm-256color', SHELL='/bin/sh', LC_ALL='C.UTF-8', **self.runtime.environment())
        fixture = Path(__file__).with_name('_native_observer_fixture.py')
        argv = [sys.executable, '-I', '-u', str(fixture), str(self.layout.runtime/'session.sock')]
        from mop.node.execution_adapters import process_identity
        def stop():
            subprocess.run([*base, 'kill-server'], capture_output=True, timeout=3)
            if not hasattr(self, 'native_pid'):
                return
            deadline = time.monotonic()+2
            while time.monotonic() < deadline:
                try:
                    if process_identity(self.native_pid) != self.native_start:
                        return
                except Unavailable:
                    return
                time.sleep(.01)
            raise AssertionError('owned native-shaped fixture survived cleanup')
        self.addCleanup(stop)
        result = await asyncio.to_thread(subprocess.run, [*base, '-f', '/dev/null', 'new-session',
            '-d', '-s', 'owned', '-c', str(self.layout.clone), 'exec '+shlex.join(argv)],
            env=environment, capture_output=True, text=True, timeout=3)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.native_record = config/'sessions/owned.json'
        async with asyncio.timeout(3):
            while not self.native_record.exists():
                await asyncio.sleep(.01)
        self.native_bytes = self.native_record.read_bytes()
        self.native_pid = json.loads(self.native_bytes)['pid']
        self.native_start = process_identity(self.native_pid)

    @asynccontextmanager
    async def final_snapshot_barrier(self, layout):
        from mop.node.native_observer import _snapshot
        loop = asyncio.get_running_loop()
        entered, finished, release = asyncio.Event(), asyncio.Event(), threading.Event()
        outcomes, snapshots = [], 0
        def snapshot(current):
            nonlocal snapshots
            if current == layout:
                snapshots += 1
                if snapshots == 3:
                    loop.call_soon_threadsafe(entered.set)
                    try:
                        if not release.wait(8):
                            raise AssertionError('test did not release owned observation worker')
                        value = _snapshot(current)
                        outcomes.append(None)
                        return value
                    except BaseException as error:
                        outcomes.append(type(error))
                        raise
                    finally:
                        loop.call_soon_threadsafe(finished.set)
            return _snapshot(current)
        with patch('mop.node.native_observer._snapshot', side_effect=snapshot):
            try:
                yield entered, finished, release, outcomes
            finally:
                release.set()
                if entered.is_set():
                    await asyncio.wait_for(finished.wait(), 3)

    async def assert_no_resurrection(self, view, epoch):
        with self.assertRaises(Unavailable):
            await view.proof(self.current)
        with self.assertRaises(Unavailable):
            await self.view()
        with self.assertRaises(Unavailable):
            await NativePhysicalSessions(owner=self.runtime).observe(self.layout)
        self.assertEqual(self.runtime._epoch, epoch, 'cancellation must not mint an epoch')
        self.assertEqual(len(self.registered), 1, 'same-lifetime ACK must never repeat')
        self.assertEqual(self.rows, [], 'no readiness or hooks may be fabricated')

    async def interrupted_observation(self, *, timeout=False, fail_worker=True):
        await self.native_process()
        await self.activate()
        view, epoch = await self.view(), self.runtime._epoch
        async with self.final_snapshot_barrier(view.layout) as (entered, finished, release, outcomes):
            task = asyncio.create_task(view.proof(self.current))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                if timeout:
                    # Настоящий production 5s timeout, без подмены часов/budget.
                    with self.assertRaises((TimeoutError, Unavailable)):
                        await asyncio.wait_for(task, 6)
                    self.assertEqual(self.runtime._state, 'closed',
                        'timed-out physical observation must latch the source before late worker completion')
                else:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                self.assertFalse(finished.is_set(), 'cancel must not pretend the worker stopped')
                if fail_worker:
                    record = json.loads(self.native_bytes)
                    self.native_record.write_text(json.dumps({**record, 'pid': self.native_pid+1}))
                release.set()
                await asyncio.wait_for(finished.wait(), 2)
                self.assertEqual(outcomes, [Unavailable] if fail_worker else [None])
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.wait_for(finished.wait(), 3)
                self.native_record.write_bytes(self.native_bytes)
        # Физическое evidence опять точное; никакой late failure уже не спасти
        # повторным snapshot. Только сохранённая invalidation запрещает старый ACK.
        await self.assert_no_resurrection(view, epoch)

    async def test_cancelled_final_snapshot_cannot_resurrect_after_late_physical_failure(self):
        await self.interrupted_observation()

    async def test_timeout_latches_source_before_late_physical_worker_failure(self):
        await self.interrupted_observation(timeout=True)

    async def test_cancelled_final_snapshot_cannot_reuse_ack_after_late_worker_success(self):
        await self.interrupted_observation(fail_worker=False)

    async def direct_close_observation(self, *, current=False):
        await self.native_process()
        await self.activate()
        view, epoch = await self.view(), self.runtime._epoch
        owner, layout = (view, view.layout) if current else (self.runtime, self.layout)
        async with self.final_snapshot_barrier(layout) as (entered, finished, release, outcomes):
            task = asyncio.create_task(NativePhysicalSessions(owner=owner).observe(layout))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await self.runtime.aclose()
                release.set()
                with self.assertRaises(Unavailable):
                    await task
                self.assertEqual(outcomes, [None], 'native evidence stayed valid across owner close')
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        await self.assert_no_resurrection(view, epoch)

    async def test_direct_observe_cannot_return_proof_after_close_during_last_snapshot(self):
        await self.direct_close_observation()

    async def test_direct_current_observe_cannot_return_proof_after_close_during_last_snapshot(self):
        await self.direct_close_observation(current=True)

    async def test_readiness_still_requires_exact_current_receipt_and_unchanged_proof(self):
        await self.activate()
        view = await self.view()
        target, _ = await view.resolve(self.current)
        calls = []
        receipt = None
        class Receipts:
            async def ready(inner, work, proof):
                calls.append(proof)
                return receipt
        ready = ReadyConfirmation(view, Receipts())
        self.assertFalse(await ready.confirmed(self.current, target))
        proof = target.session_proof
        receipt = ReadyReceipt(self.registration, proof.scope, proof.pid, proof.process_start,
                               str(uuid4()), 'owned-model')
        self.assertFalse(await ready.confirmed(self.current, target), 'lifetime receipt is not current ready proof')
        receipt = replace(receipt, registration=self.current_registration)
        self.assertTrue(await ready.confirmed(self.current, target))
        class RevokingReceipts:
            async def ready(inner, work, proof):
                self.revoked = True
                return receipt
        with self.assertRaises(Refused):
            await ReadyConfirmation(view, RevokingReceipts()).confirmed(self.current, target)
        self.assertEqual(self.rows, [])
        self.assertEqual(len(self.registered), 1)


if __name__ == '__main__':
    unittest.main()
