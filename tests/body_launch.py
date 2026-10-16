#!/usr/bin/env python3
"""HYPOTHESIS: запуск читает файловые markers вместо addressed assignment.
SOLUTION: immutable registration/layout, exact API response и physical/session ports.
RESULT: воспроизведены отсутствие addressed boundary/readiness/producer/probe,
Unix pathname overflow (121 > 100), bool/schema и control characters, затем
11 checks зелёные. Настоящий owned
process/Unix socket и stdlib hook IPC проверены; server receipts/image/routes —
контролируемые doubles, production SessionApi/PVE не подтверждены.
STATUS: FIXED — see #643 (driver/node stage, Root binding отдельно)
"""
import asyncio
import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import UUID, uuid4

import hermetic  # noqa: F401
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mop.common import soul
from mop.node.http_execution import Work, Refused, Unavailable
from mop.node.execution_adapters import BoundTarget, ActionInput


def api():
    spec = importlib.util.find_spec('mop.driver.launch')
    assert spec is not None, 'addressed body launch boundary is missing'
    from mop.driver import launch
    return launch


def work():
    return Work.parse(dict(schema_version=1, **{key: str(uuid4()) for key in
        ('epoch', 'operation_id', 'step_id', 'target_id', 'incarnation_id', 'node_id',
         'executor_epoch', 'grant_id')}, owner_id='1', generation='2', revision='3',
        role='worker', bundle_digest='a'*64, action='apply_identity',
        not_after=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()))


def bundle():
    return soul.from_manifest(dict(version=1, soul='worker',
        prompt='prompt.md', runtime=dict(driver='any', receives_dispatch=True),
        capabilities=dict(node_verbs=[], cluster_verbs=[], dispatch=False, spawn=[]),
        secrets=dict(files=[], vars=[])), b'approved prompt\n')


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
        with tempfile.TemporaryDirectory(prefix='mop-643-') as directory:
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


class PhysicalProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_owned_process_socket_and_scope_are_required_not_a_stale_session_file(self):
        m = api()
        from mop.node import body_launch
        self.assertTrue(hasattr(body_launch, 'SessionApiProbe'), 'physical SessionApi probe is missing')
        from mop.node.body_launch import SessionApiProbe, SessionProof, process_identity
        w = work()
        r = m.Registration(w, str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        with tempfile.TemporaryDirectory(prefix='m643-') as directory:
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
            layout.tmux_socket, '%0', layout.launcher, proof.session_socket)
        receipt = ReadyReceipt(r, proof.scope, proof.pid, proof.process_start, str(uuid4()), 'confirmed-model')
        class Receipts:
            async def ready(self, requested, observed): return receipt
        confirmation = ReadyConfirmation(resolver, Receipts())
        self.assertTrue(await confirmation.confirmed(w, target))
        for bad in (replace(receipt, model=None), replace(receipt, process_start='old-start'),
                    replace(receipt, scope=replace(proof.scope, producer_epoch=uuid4()))):
            receipt = bad
            self.assertFalse(await confirmation.confirmed(w, target))
        receipt = ReadyReceipt(r, proof.scope, proof.pid, proof.process_start, str(uuid4()), 'confirmed-model')
        class RacingReceipts:
            async def ready(self, requested, observed):
                nonlocal proof
                proof = replace(proof, process_start='replacement-during-HTTP')
                return receipt
        self.assertFalse(await ReadyConfirmation(resolver, RacingReceipts()).confirmed(w, target))

    async def test_new_producer_is_registered_before_private_hook_bridge_is_exposed(self):
        m = api()
        from mop.node import body_launch
        self.assertTrue(hasattr(body_launch, 'SessionRuntime'), 'registered producer lifetime is missing')
        from mop.node.body_launch import SessionRuntime, SessionProof
        from mop.common.telemetry import Ack
        w = work()
        r = m.Registration(w, str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        with tempfile.TemporaryDirectory(prefix='m643-') as directory:
            path = Path(directory)/'hook.sock'
            os.chmod(directory, 0o700)
            proof = SessionProof(r, '%0', 'actual-session', uuid4(), Path(directory)/'session.sock', 123, 'start-1')
            events = []
            class Source:
                async def register(self, observed):
                    events.append(('registered', observed.scope))
                    self.scope = observed.scope
                    return observed.scope
                async def ingest(self, observation):
                    self.observation = observation
                    return Ack(observation.scope.producer_epoch, observation.sequence)
            source = Source()
            runtime = SessionRuntime(source, source)
            bridge = await runtime.attach(proof, path=path)
            try:
                self.assertEqual(events, [('registered', proof.scope)])
                env = bridge.environment()
                self.assertEqual(set(env), {'MOP_TELEMETRY_SOCKET', 'MOP_TELEMETRY_TOKEN'})
                self.assertEqual(len(env['MOP_TELEMETRY_TOKEN']), 64)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                from mop import session
                from unittest.mock import patch
                with patch.dict(os.environ, env):
                    await asyncio.to_thread(session.hook, json.dumps(dict(session_id='actual-session',
                        hook_event_name='SessionStart')), 0)
                for _ in range(100):
                    if bridge.buffered: break
                    await asyncio.sleep(.001)
                await bridge.flush()
                self.assertEqual(source.observation.scope, proof.scope)
            finally:
                await runtime.aclose()
            self.assertFalse(path.exists())
            class WrongSource:
                async def register(self, observed):
                    return replace(observed.scope, producer_epoch=uuid4())
            runtime = SessionRuntime(WrongSource(), source)
            with self.assertRaises(Refused): await runtime.attach(proof, path=path)
            self.assertFalse(path.exists())
            await runtime.aclose()


if __name__ == '__main__': unittest.main()
