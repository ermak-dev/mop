"""HOST physical backend epoch ownership, not vendor-native acceptance #745.

HYPOTHESIS: frontend-local uuid4 mistakes a transport restart for loss of the
physical executor. Raw JSON/SQL UUID is not proof of that executor's lifetime.
SPEC: process-owned nonce, immutable identity, live one-time epoch bind; same
backend resumes without effects, unknown old inventory refuses before CAS.
SOLUTION: finite root Unix session, process-owned nonce and retained ownership.
RESULT: missing-boundary, peer-client and inventory/source-closure RED observed;
unit state/transport negatives GREEN. Actual root/systemd and HTTP/PG separately.
STATUS: FIXED — see #745
"""
import asyncio
from dataclasses import replace
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import _lib  # noqa: F401
import hermetic  # noqa: F401
from host_scope import Adapter, installed, scope
from mop.node.host_scope import Refused, Request, Unknown
from mop.node.host_scope_supervisor import Supervisor, SocketServer

INSTALLATION = 'host-backend-fixture'
NODE, EPOCH, PREVIOUS = (str(uuid4()) for _ in range(3))


def api():
    assert importlib.util.find_spec('mop.node.host_backend') is not None, \
        'concrete physical backend authority boundary is missing (#745)'
    from mop.node import host_backend
    return host_backend


class InventoryAdapter(Adapter):
    """Unit OS port only; actual Unix/systemd checks live in scope-probe."""
    def __init__(self):
        super().__init__()
        self.inventory = ()
        self.inventory_calls = 0

    async def startup_inventory(self):
        self.inventory_calls += 1
        return self.inventory


class BackendOwnerTests(unittest.IsolatedAsyncioTestCase):
    def owner(self):
        m = api()
        adapter = InventoryAdapter()
        owner = Supervisor(installed(), adapter, identity=m.Identity(INSTALLATION, NODE), timeout=.2)
        self.addAsyncCleanup(owner.aclose)
        return m, owner, adapter

    def request(self, action, *, epoch=None, executor=None, empty=None, **change):
        m = api()
        return m.BackendRequest(action, str(uuid4()),
            m.Identity(change.get('installation', INSTALLATION), change.get('node', NODE)),
            epoch, executor, empty)

    async def bind(self, owner, **change):
        return await owner.backend(self.request('backend_bind', epoch=EPOCH,
            executor=PREVIOUS, empty=True, **change))

    async def test_backend_nonce_self_generated_once_and_kernel_process_identity(self):
        m, owner, adapter = self.owner()
        one = await owner.backend(self.request('backend_query'))
        two = await owner.backend(self.request('backend_query'))
        self.assertEqual(one, two)
        self.assertNotEqual(one.executor_epoch, PREVIOUS)
        self.assertIsNone(one.bound_epoch)
        self.assertEqual(one.process, m.Process.current())
        self.assertEqual(one.process.pid, os.getpid())
        self.assertEqual(adapter.calls, [])
        self.assertEqual(adapter.inventory_calls, 0)
        with self.assertRaises((TypeError, Refused)):
            m.VerifiedAuthority(one.wire())

    async def test_empty_first_bind_once_and_same_backend_resume_never_inventory_or_kill(self):
        m, owner, adapter = self.owner()
        first = await self.bind(owner)
        self.assertEqual(first.mode, 'first')
        self.assertEqual(first.bound_epoch, EPOCH)
        self.assertEqual(adapter.inventory_calls, 1)
        confirmed = await owner.backend(self.request('backend_confirm', epoch=EPOCH,
            executor=first.executor_epoch, empty=True))
        self.assertEqual(confirmed.mode, 'resume')
        resumed = await owner.backend(self.request('backend_bind', epoch=EPOCH,
            executor=first.executor_epoch, empty=False))
        self.assertEqual(resumed.executor_epoch, first.executor_epoch)
        self.assertEqual(resumed.mode, 'resume')
        self.assertEqual(adapter.inventory_calls, 1)
        self.assertEqual(adapter.calls, [])

    async def test_bound_backend_never_rebinds_restore_epoch_or_retries_lost_cas(self):
        m, owner, adapter = self.owner()
        first = await self.bind(owner)
        for epoch, executor in ((str(uuid4()), first.executor_epoch), (EPOCH, PREVIOUS),
                                (EPOCH, str(uuid4()))):
            with self.subTest(epoch=epoch, executor=executor), self.assertRaises((Refused, Unknown)):
                await owner.backend(self.request('backend_bind', epoch=epoch,
                    executor=executor, empty=True))
        self.assertEqual(adapter.inventory_calls, 1)
        self.assertEqual(adapter.calls, [])
        self.assertEqual((await owner.backend(self.request('backend_query'))).bound_epoch, EPOCH)

    async def test_first_bind_refuses_sql_active_unknown_old_inventory_or_pending_fence(self):
        m = api()
        for kind in ('sql-active', 'old-populated', 'foreign-empty', 'unknown-inventory', 'fence', 'inflight'):
            with self.subTest(kind=kind):
                m, owner, adapter = self.owner()
                if kind in ('old-populated', 'foreign-empty'):
                    address = scope()
                    facts = await adapter.start(address)
                    adapter.inventory = (replace(facts, populated=kind == 'old-populated'),)
                    adapter.calls.clear()
                if kind == 'unknown-inventory':
                    adapter.inventory = None
                if kind in ('fence', 'inflight'):
                    owner._fences.add(scope())
                with self.assertRaises((Refused, Unknown)):
                    await owner.backend(self.request('backend_bind', epoch=EPOCH,
                        executor=PREVIOUS, empty=kind != 'sql-active'))
                self.assertEqual(adapter.calls, [])
                self.assertIsNone((await owner.backend(self.request('backend_query'))).bound_epoch)

    async def test_wrong_installed_node_installation_never_binds_or_enumerates(self):
        m, owner, adapter = self.owner()
        for change in ({'node': str(uuid4())}, {'installation': 'other'}):
            with self.assertRaisesRegex(Refused, 'identity|installation|node'):
                await self.bind(owner, **change)
        self.assertEqual(adapter.inventory_calls, 0)

    async def test_configured_start_requires_bound_installed_node_epoch_and_backend_nonce(self):
        m, owner, adapter = self.owner()
        current = await owner.backend(self.request('backend_query'))
        s = replace(scope(), node_id=NODE, epoch=EPOCH, executor_epoch=current.executor_epoch)
        with self.assertRaisesRegex(Refused, 'bound|authority'):
            await owner.execute(installed().node_uid, Request('start', s, str(uuid4())))
        first = await self.bind(owner)
        await owner.backend(self.request('backend_confirm', epoch=EPOCH,
            executor=first.executor_epoch, empty=True))
        for change in ({'node_id': str(uuid4())}, {'epoch': str(uuid4())},
                       {'executor_epoch': str(uuid4())}):
            with self.assertRaisesRegex(Refused, 'authority|epoch|node'):
                await owner.execute(installed().node_uid, Request('start', replace(s, **change), str(uuid4())))
        self.assertEqual(adapter.calls, [])

    async def test_raw_dictionary_or_fake_backend_cannot_bypass_native_startup(self):
        from mop.node import native_startup
        from native_startup import Client, Executor
        client = Client()
        executor = Executor(client.calls)
        with self.assertRaisesRegex((Refused, TypeError), 'backend|authority'):
            await native_startup.start(client, executor, installation_id=INSTALLATION,
                node_id=NODE, backend={'executor_epoch': PREVIOUS})
        self.assertNotIn('restart', [row[0] for row in client.calls])

    async def test_authority_wire_is_finite_not_work_env_argv_or_nonce_input(self):
        m = api()
        request = self.request('backend_bind', epoch=EPOCH, executor=PREVIOUS, empty=True)
        self.assertEqual(m.BackendRequest.decode(request.encode()), request)
        from mop.node.host_scope import encode
        for change in ({'backend_nonce': str(uuid4())}, {'env': {}}, {'argv': []}, {'policy': {}},
                       {'schema_version': True}, {'action': 'exec'}, {'empty': 1}):
            with self.assertRaises(Refused):
                m.BackendRequest.decode(encode({**request.wire(), **change}))
        for action, epoch, executor, empty in (
                ('backend_query', EPOCH, None, None), ('backend_bind', None, PREVIOUS, True),
                ('backend_confirm', EPOCH, PREVIOUS, None)):
            with self.assertRaises(Refused):
                self.request(action, epoch=epoch, executor=executor, empty=empty)


class BackendInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cold_inventory_requires_both_empty_manager_and_empty_kernel_slice(self):
        from mop.node import host_scope_systemd as systemd
        self.assertTrue(callable(getattr(systemd.SystemdCgroupAdapter, 'startup_inventory', None)),
                        'cold bind needs bounded physical inventory, not a callback returning True')
        import json
        adapter = object.__new__(systemd.SystemdCgroupAdapter)
        class Commands:
            raw = b'[]'
            calls = []
            async def run(self, *words):
                self.calls.append(words)
                return self.raw
        adapter.command = Commands()
        with tempfile.TemporaryDirectory(prefix='mop-backend-inventory-') as directory:
            root = Path(directory)
            original_open = systemd.open_directory
            def unit_directory(path, **kwargs):
                return original_open(path, owner=os.getuid())
            with patch.object(systemd, 'CGROUP_ROOT', root), patch.object(systemd, 'open_directory', unit_directory):
                self.assertEqual(await adapter.startup_inventory(), ())
                child = root/'mop.slice/mop-host.slice'
                child.mkdir(parents=True)
                child.chmod(0o755)
                (child/'cgroup.events').write_text('populated 0\nfrozen 0\n')
                (child/'cgroup.procs').write_text('')
                self.assertEqual(await adapter.startup_inventory(), ())
                (child/scope().unit).mkdir()
                self.assertNotEqual(await adapter.startup_inventory(), ())
                for entry in child.iterdir():
                    if entry.is_dir(): entry.rmdir()
                adapter.command.raw = json.dumps([dict(unit=scope().unit)]).encode()
                self.assertNotEqual(await adapter.startup_inventory(), ())
                for raw in (b'{}', b'null', b'[{"unit":"foreign.service"}]', b'['*10000):
                    adapter.command.raw = raw
                    with self.assertRaises((Refused, Unknown)):
                        await adapter.startup_inventory()
        self.assertTrue(all('list-units' in words and 'mop-host-*.service' in words
                            for words in adapter.command.calls))


class BackendTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonroot_json_server_is_not_verified_root_authority(self):
        self.assertIsNotNone(importlib.util.find_spec('mop.node.host_backend_client'),
                             'authenticated concrete HOST backend client is missing')
        from mop.node.host_backend_client import BackendClient
        if os.geteuid() == 0:
            self.skipTest('non-root unit process required; actual root pair is scope-probe')
        with tempfile.TemporaryDirectory(prefix='mop-backend-peer-') as directory:
            path = Path(directory)/'authority.sock'
            async def fake(reader, writer):
                try:
                    writer.write(b'{"executor_epoch":"'+PREVIOUS.encode()+b'"}\n')
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()
            server = await asyncio.start_unix_server(fake, path=str(path))
            try:
                client = BackendClient(path, node_uid=os.getuid(),
                    identity=api().Identity(INSTALLATION, NODE))
                with self.assertRaisesRegex(Refused, 'root|immutable|peer'):
                    async with client.connect():
                        self.fail('nonroot JSON became verified authority')
            finally:
                server.close()
                await server.wait_closed()

    async def test_authority_disconnect_does_not_close_supervisor_or_retire_acknowledged_body(self):
        m = api()
        owner = Supervisor(installed(), InventoryAdapter(), identity=m.Identity(INSTALLATION, NODE))
        first = await owner.backend(m.BackendRequest('backend_bind', str(uuid4()), owner.identity,
            EPOCH, PREVIOUS, True))
        await owner.backend(m.BackendRequest('backend_confirm', str(uuid4()), owner.identity,
            EPOCH, first.executor_epoch, True))
        s = replace(scope(), node_id=NODE, epoch=EPOCH, executor_epoch=first.executor_epoch)
        snap = await owner.execute(installed().node_uid, Request('start', s, str(uuid4())))
        before = tuple(owner.adapter.calls)
        # Повторный frontend attach не reconcile/retire и не remint body reference.
        resumed = await owner.backend(m.BackendRequest('backend_bind', str(uuid4()), owner.identity,
            EPOCH, first.executor_epoch, False))
        self.assertEqual(resumed.executor_epoch, first.executor_epoch)
        self.assertEqual(tuple(owner.adapter.calls), before)
        self.assertEqual(owner._owned[s], snap.reference)
        self.assertTrue(owner.adapter.units[s].populated)
        self.assertFalse(owner._closed)
        await owner.aclose()


if __name__ == '__main__':
    unittest.main()
