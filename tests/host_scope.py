#!/usr/bin/env python3
"""HYPOTHESIS: PID/pane и название unit не доказывают HOST containment.
SPEC: finite UUID protocol, installed peer authority, exact OS facts, bounded
I/O, admission перед socket, single action/cleanup owner при cancellation.
SOLUTION: отдельный node port без связи с launch/observer и installer.
RESULT: сначала 16 RED для отсутствующего port, затем отдельные RED для
lost ACK, output/reap, admission binding/deadline, unit/ExecStartEx facts и
post-launch mismatch. 34 narrow checks GREEN; private systemd/cgroup-v2
fixture отдельно доказала double-fork/setsid containment и empty retirement.
Fake adapter не OS acceptance; native/real HTTP admission не проверялись.
STATUS: FIXED — see #719
"""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from uuid import uuid4

import _lib  # noqa: F401
import hermetic  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def api():
    assert importlib.util.find_spec('mop.node.host_scope') is not None, \
        'finite HOST containment boundary is missing (#719)'
    from mop.node import host_scope
    return host_scope


def scope():
    m = api()
    return m.Scope(*(str(uuid4()) for _ in range(6)))


def installed():
    m = api()
    return m.Installed(node_uid=1101, body_uid=1102, body_gid=1102,
                       runner=Path('/opt/mop/libexec/host-runner'),
                       body_root=Path('/var/lib/mop/host-bodies'),
                       node_private=Path('/var/lib/mop/node-service'))


class Adapter:
    """Только fake OS port: никогда не доказательство containment."""
    def __init__(self):
        self.units = {}
        self.calls = []
        self.entered = asyncio.Event()
        self.release = None
        self.mismatch = False
        self.kill_error = False

    async def inspect(self, address):
        self.calls.append(('inspect', address))
        return self.units.get(address)

    async def start(self, address):
        m = api()
        self.calls.append(('start', address))
        self.entered.set()
        if self.release:
            await self.release.wait()
        facts = m.Facts(address, uuid4().hex, str(uuid4()), 1102, 1102,
                        str(installed().runner), (str(installed().runner), '--scope', address.unit),
                        address.cgroup, True, True)
        self.units[address] = facts
        return facts

    async def retire(self, reference):
        m = api()
        self.calls.append(('retire', reference))
        if self.kill_error:
            raise m.Unknown('cgroup read unavailable; outcome unknown')
        self.units[reference.scope] = replace(self.units[reference.scope], populated=False)
        return self.units[reference.scope]


class ContractTests(unittest.TestCase):
    def test_scope_names_and_paths_derive_only_from_canonical_uuids(self):
        s = scope()
        self.assertLess(len(s.unit), 256)
        self.assertLess(len('systemd-private-'+uuid4().hex+'-'+s.unit+'-abcdef'), 256,
                        'PrivateTmp generated component must fit Linux NAME_MAX')
        self.assertEqual(s.cgroup, '/mop.slice/mop-host.slice/' + s.unit,
                         'systemd dash-separated slices have parent slices')
        self.assertEqual(api().Scope.parse(s.wire()), s)
        self.assertEqual(api().Scope.from_unit(s.unit), s)
        for bad in ('../foreign', str(uuid4()).upper(), '', 1, True, None):
            with self.subTest(bad=bad), self.assertRaises(api().Refused):
                replace(s, launch_id=bad)
        for bad in ('mop-host-foreign.service', s.unit + '/x', s.unit.replace('.service', '.scope')):
            with self.assertRaises(api().Refused):
                api().Scope.from_unit(bad)

    def test_installed_authority_separates_node_and_body_and_has_no_secret_fields(self):
        p = installed()
        for changes in ({'node_uid': p.body_uid}, {'body_uid': 0}, {'node_uid': 0},
                        {'body_gid': 0}, {'runner': Path('/tmp/../bin/sh')},
                        {'runner': Path('relative')}, {'body_root': Path('/')},
                        {'body_root': p.node_private}):
            with self.subTest(changes=changes), self.assertRaises(api().Refused):
                replace(p, **changes)
        self.assertEqual(set(p.__dataclass_fields__),
                         {'node_uid', 'body_uid', 'body_gid', 'runner', 'body_root', 'node_private'})

    def test_finite_wire_rejects_work_authority_extra_env_paths_and_future_schema(self):
        m, s = api(), scope()
        request = m.Request('start', s, str(uuid4()))
        self.assertEqual(m.Request.decode(request.encode()), request)
        for extra in ({'argv': ['/bin/sh']}, {'env': {'TOKEN': 'x'}}, {'path': '/tmp/x'},
                      {'work': {'node_uid': 1101}}, {'schema_version': 2}, {'action': 'exec'}):
            with self.subTest(extra=extra), self.assertRaises(m.Refused):
                m.Request.decode(json.dumps({**request.wire(), **extra}).encode())
        for raw in (b'{}', b'[]', b'{"schema_version":1,"schema_version":1}',
                    b'x' * (m.FRAME_LIMIT + 1), b'\xff'):
            with self.assertRaises(m.Refused):
                m.Request.decode(raw)
        with self.assertRaises(m.Refused):
            m.Request('retire', s, str(uuid4()))
        with self.assertRaises(m.Refused):
            m.Request('start', s, str(uuid4()), m.Reference(s, uuid4().hex, str(uuid4())))

    def test_actual_facts_not_name_prefix_are_required(self):
        m, s, p = api(), scope(), installed()
        f = m.Facts(s, uuid4().hex, str(uuid4()), p.body_uid, p.body_gid,
                    str(p.runner), (str(p.runner), '--scope', s.unit), s.cgroup, True, True)
        f.verify(p, s)
        for changes in ({'uid': p.node_uid}, {'gid': 0}, {'executable': '/bin/sh'},
                        {'argv': ('/bin/sh',)}, {'cgroup': '/foreign/' + s.unit},
                        {'invocation_id': '0'*32}, {'boot_id': 'bad'},
                        {'nondelegated': False}, {'scope': scope()}):
            with self.subTest(changes=changes), self.assertRaises(m.Refused):
                replace(f, **changes).verify(p, s)


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        m = api()
        from mop.node.host_scope_supervisor import Supervisor
        self.adapter, self.s, self.p = Adapter(), scope(), installed()
        self.owner = Supervisor(self.p, self.adapter, capacity=2, timeout=.3)
        self.m = m

    def request(self, action='start', reference=None, address=None):
        return self.m.Request(action, address or self.s, str(uuid4()), reference)

    async def start(self):
        return await self.owner.execute(self.p.node_uid, self.request())

    async def test_wrong_peer_never_calls_os_even_with_forged_work(self):
        for uid in (0, self.p.body_uid, self.p.node_uid + 10):
            with self.assertRaisesRegex(self.m.Refused, 'peer'):
                await self.owner.execute(uid, self.request())
        self.assertEqual(self.adapter.calls, [])

    async def test_start_inspect_retire_require_exact_invocation_and_no_replay(self):
        snap = await self.start()
        self.assertTrue(snap.populated)
        with self.assertRaisesRegex(self.m.Refused, 'exists|collision|replay'):
            await self.start()
        inspected = await self.owner.execute(self.p.node_uid, self.request('inspect', snap.reference))
        self.assertEqual(inspected, snap)
        empty = await self.owner.execute(self.p.node_uid, self.request('retire', snap.reference))
        self.assertFalse(empty.populated)

    async def test_collision_uid_exec_cgroup_or_invocation_never_stops_foreign_unit(self):
        snap = await self.start()
        original = self.adapter.units[self.s]
        for changes in ({'uid': 0}, {'argv': ('/bin/sh',)}, {'cgroup': '/foreign'},
                        {'invocation_id': uuid4().hex}, {'boot_id': str(uuid4())}):
            self.adapter.units[self.s] = replace(original, **changes)
            with self.assertRaises(self.m.Refused):
                await self.owner.execute(self.p.node_uid, self.request('retire', snap.reference))
            self.assertFalse(any(c[0] == 'retire' for c in self.adapter.calls))
        self.adapter.units[self.s] = original
        for reference in (replace(snap.reference, invocation_id=uuid4().hex),
                          replace(snap.reference, boot_id=str(uuid4()))):
            with self.assertRaises(self.m.Refused):
                await self.owner.execute(self.p.node_uid, self.request('retire', reference))
        self.assertFalse(any(c[0] == 'retire' for c in self.adapter.calls))

    async def test_absent_unit_is_unknown_not_quiescence_or_restart_anchor(self):
        ref = self.m.Reference(self.s, uuid4().hex, str(uuid4()))
        with self.assertRaisesRegex(self.m.Unknown, 'absent|unavailable|unknown'):
            await self.owner.execute(self.p.node_uid, self.request('inspect', ref))
        self.assertFalse(any(c[0] == 'start' for c in self.adapter.calls))

    async def test_concurrent_scope_action_refuses_and_cancellation_keeps_cleanup_owner(self):
        self.adapter.release = asyncio.Event()
        task = asyncio.create_task(self.start())
        await self.adapter.entered.wait()
        with self.assertRaisesRegex(self.m.Refused, 'active|busy'):
            await self.start()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.owner.active)
        self.adapter.release.set()
        await self.owner.aclose()
        self.assertFalse(self.adapter.units[self.s].populated)
        self.assertEqual(sum(c[0] == 'start' for c in self.adapter.calls), 1)
        self.assertFalse(self.owner.active)

    async def test_post_launch_validation_refusal_is_unknown_and_never_kills_a_mismatch(self):
        original = self.adapter.start
        async def malformed(address):
            facts = replace(await original(address), uid=0)
            self.adapter.units[address] = facts
            return facts
        self.adapter.start = malformed
        with self.assertRaisesRegex(self.m.Unknown, 'mismatch'):
            await self.start()
        self.assertFalse(any(c[0] == 'retire' for c in self.adapter.calls))
        with self.assertRaises(self.m.Refused):
            await self.start()

    async def test_start_lost_os_ack_with_proven_reference_cleans_up_and_stays_unknown(self):
        # Здесь OS уже доказал reference; случай без snapshot/proof ниже обязан
        # остаться unknown без adoption/kill даже при matching позднем unit.
        original = self.adapter.start
        async def lost(address):
            facts = await original(address)
            raise self.m.StartUnknown('launch ACK lost', facts.verify(self.p, address).reference)
        self.adapter.start = lost
        with self.assertRaisesRegex(self.m.Unknown, 'ACK lost'):
            await self.start()
        self.assertFalse(self.adapter.units[self.s].populated)
        with self.assertRaises(self.m.Refused):
            await self.start()

    async def test_unknown_start_without_snapshot_never_adopts_a_later_invocation(self):
        # HYPOTHESIS (#741): start Unknown не несёт reference; повторный inspect
        # может усыновить уже другую incarnation и выдать ей cgroup.kill.
        # SOLUTION: cleanup только по pinned StartUnknown.reference; без proof
        # остаются fence и громкий unknown, но нет чужого destructive effect.
        # RESULT: RED выдавал retire другой invocation; GREEN не вызывает kill.
        # STATUS: FIXED — see #741
        original = self.adapter.start
        async def replaced_before_observation(address):
            original_facts = await original(address)
            self.adapter.units[address] = replace(original_facts, invocation_id=uuid4().hex)
            raise self.m.Unknown('start inspection unavailable; no snapshot')
        self.adapter.start = replaced_before_observation
        with self.assertRaisesRegex(self.m.Unknown, 'no snapshot'):
            await self.start()
        self.assertFalse(any(c[0] == 'retire' for c in self.adapter.calls),
                         'no initial reference means no authority to adopt a later unit')
        self.assertTrue(self.adapter.units[self.s].populated)
        self.assertIn(self.s, self.owner._fences)
        with self.assertRaises(self.m.Refused):
            await self.start()
        self.assertIn('unknown', self.owner.last_error)
        with self.assertRaises(self.m.Unknown):
            await self.owner.aclose()
        self.assertEqual(sum(c[0] == 'start' for c in self.adapter.calls), 1)

    async def test_pinned_unknown_start_never_cleans_a_changed_invocation_or_missing_snapshot(self):
        for observation in ('changed', 'unavailable', 'absent'):
            with self.subTest(observation=observation):
                await self.asyncSetUp()
                original_start, original_inspect = self.adapter.start, self.adapter.inspect
                async def lost(address):
                    facts = await original_start(address)
                    reference = facts.verify(self.p, address).reference
                    if observation == 'changed':
                        self.adapter.units[address] = replace(facts, invocation_id=uuid4().hex)
                    raise self.m.StartUnknown('start observation unknown', reference)
                async def inspect(address):
                    if self.adapter.entered.is_set():
                        if observation == 'unavailable':
                            raise self.m.Unknown('kernel snapshot unavailable')
                        if observation == 'absent':
                            return None
                    return await original_inspect(address)
                self.adapter.start, self.adapter.inspect = lost, inspect
                with self.assertRaisesRegex(self.m.Unknown, 'observation unknown'):
                    await self.start()
                self.assertTrue(self.adapter.units[self.s].populated)
                self.assertFalse(any(c[0] == 'retire' for c in self.adapter.calls))
                self.assertIn(self.s, self.owner._fences)
                with self.assertRaises(self.m.Refused):
                    await self.start()
                self.assertIsNotNone(self.owner.last_error)
                with self.assertRaises(self.m.Unknown):
                    await self.owner.aclose()
                self.assertEqual(sum(c[0] == 'start' for c in self.adapter.calls), 1)

    async def test_unknown_cleanup_retains_fence_and_never_reports_empty(self):
        snap = await self.start()
        self.adapter.kill_error = True
        with self.assertRaises(self.m.Unknown):
            await self.owner.execute(self.p.node_uid, self.request('retire', snap.reference))
        with self.assertRaises(self.m.Refused):
            await self.start()
        self.assertTrue(self.adapter.units[self.s].populated)

    async def test_bounded_capacity_and_timeout_own_inflight_start(self):
        self.adapter.release = asyncio.Event()
        task = asyncio.create_task(self.start())
        await self.adapter.entered.wait()
        with self.assertRaises(self.m.Unknown):
            await task
        with self.assertRaises(self.m.Refused):
            await self.start()
        self.adapter.release.set()
        await self.owner.aclose()
        self.assertFalse(self.adapter.units[self.s].populated)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_guard_runs_before_any_socket_and_after_admission_await(self):
        m = api()
        from mop.node.host_scope_client import Client
        from mop.node.http_execution import EffectGuard
        calls = []
        async def authorize():
            calls.append('authorize')
        def local_fence():
            calls.append('check')
            raise m.Refused('stale current epoch')
        client = Client(Path('/nonexistent/719.sock'), node_uid=os.getuid())
        with self.assertRaisesRegex(m.Refused, 'stale'):
            await client.start(scope(), EffectGuard(authorize, local_fence))
        self.assertEqual(calls, ['authorize', 'check'])
        async def denied():
            raise m.Refused('server admission refused')
        with self.assertRaisesRegex(m.Refused, 'admission'):
            await client.start(scope(), EffectGuard(denied, lambda: None))

    async def test_admission_wait_is_bounded_and_expired_work_cannot_reach_socket(self):
        from mop.node.host_scope_client import Client
        from mop.node.http_execution import EffectGuard
        from _body_fixture import work
        from datetime import datetime, timedelta, timezone
        m, w = api(), work()
        s = m.Scope(w.epoch, w.node_id, w.executor_epoch, w.target_id,
                    w.incarnation_id, str(uuid4()))
        client = Client(Path('/nonexistent/719.sock'), node_uid=os.getuid(), work=w, timeout=.05)
        async def stuck():
            await asyncio.Event().wait()
        async with asyncio.timeout(.5):
            with self.assertRaisesRegex(m.Unknown, 'admission'):
                await client.start(s, EffectGuard(stuck, lambda: None))
        expired = replace(w, not_after=datetime.now(timezone.utc)-timedelta(seconds=1))
        client = Client(Path('/nonexistent/719.sock'), node_uid=os.getuid(), work=expired)
        async def allow():
            pass
        with self.assertRaisesRegex(m.Refused, 'expired'):
            await client.start(s, EffectGuard(allow, lambda: None))

    async def test_scope_must_match_current_authenticated_work_before_socket(self):
        from mop.node.host_scope_client import Client
        from mop.node.http_execution import EffectGuard
        from _body_fixture import work
        m, s, w = api(), scope(), work()
        async def allow():
            pass
        client = Client(Path('/nonexistent/719.sock'), node_uid=os.getuid(), work=w)
        with self.assertRaisesRegex(m.Refused, 'work|epoch|address'):
            await client.start(s, EffectGuard(allow, lambda: None))
        current = m.Scope(w.epoch, w.node_id, w.executor_epoch, w.target_id,
                          w.incarnation_id, str(uuid4()))
        with self.assertRaises(m.Unknown):
            await client.start(current, EffectGuard(allow, lambda: None))

    async def test_unbound_port_never_claims_containment_capability(self):
        api()
        from mop.node.host_scope_client import UnboundHostScopes
        m = api()
        with self.assertRaisesRegex(m.Unknown, 'not bound'):
            await UnboundHostScopes().inspect(m.Reference(scope(), uuid4().hex, str(uuid4())), None)
        self.assertFalse(getattr(UnboundHostScopes(), 'scope_retirement_v1', False))


class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.m = api()
        from mop.node.host_scope_supervisor import Supervisor, SocketServer
        self.directory = tempfile.TemporaryDirectory(prefix='m719-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'supervisor.sock'
        self.p = replace(installed(), node_uid=os.getuid(), body_uid=os.getuid()+1,
                         body_gid=os.getgid()+1)
        self.adapter = Adapter()
        async def start(address):
            facts = await Adapter.start(self.adapter, address)
            facts = replace(facts, uid=self.p.body_uid, gid=self.p.body_gid)
            self.adapter.units[address] = facts
            return facts
        self.adapter.start = start
        self.owner = Supervisor(self.p, self.adapter, timeout=.5)
        self.server = SocketServer(self.owner, self.path, timeout=.2)
        await self.server.start()
        self.addAsyncCleanup(self.server.aclose)

    async def test_real_unix_peer_and_start_ack_round_trip(self):
        from mop.node.host_scope_client import Client
        from mop.node.http_execution import EffectGuard
        async def allow():
            pass
        from _body_fixture import work
        w = work()
        s = self.m.Scope(w.epoch, w.node_id, w.executor_epoch, w.target_id,
                         w.incarnation_id, str(uuid4()))
        client = Client(self.path, node_uid=os.getuid(), supervisor_uid=os.getuid(), timeout=.5, work=w)
        snap = await client.start(s, EffectGuard(allow, lambda: None))
        self.assertTrue(snap.populated)
        await self.server.drain()
        self.assertFalse(self.owner._receipts)
        self.assertTrue(self.adapter.units[s].populated, 'exact ACK transfers ownership, not retirement')
        empty = await client.retire(snap.reference, EffectGuard(allow, lambda: None))
        self.assertFalse(empty.populated)

    async def test_lost_ack_retires_only_the_started_scope(self):
        s = scope()
        reader, writer = await asyncio.open_unix_connection(str(self.path))
        writer.write(self.m.Request('start', s, str(uuid4())).encode()+b'\n')
        await writer.drain()
        response = self.m.decode((await reader.readline()).rstrip(b'\n'))
        self.assertEqual(response['status'], 'ok')
        writer.close()
        await writer.wait_closed()
        await self.server.drain()
        self.assertFalse(self.adapter.units[s].populated)
        self.assertEqual(sum(c[0] == 'retire' for c in self.adapter.calls), 1)

    async def lost_ack_during_inspection(self, *, loss='disconnect', foreign=False,
                                         kill_error=False, stalled=False):
        """Real Unix sockets + barriers; fake OS facts, не containment proof."""
        s = scope()
        request = self.m.Request('start', s, str(uuid4()))
        reader, writer = await asyncio.open_unix_connection(str(self.path))
        self.addCleanup(writer.close)
        writer.write(request.encode()+b'\n')
        await writer.drain()
        response = self.m.decode((await reader.readline()).rstrip(b'\n'))
        self.assertEqual(response['status'], 'ok')
        snapshot = self.m.Snapshot.parse(response['snapshot'])
        self.assertIn(s, self.owner._receipts, 'successful start stays owned until exact ACK')
        start_handler, = self.server._handlers
        entered, release, abandoning = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_inspect = self.adapter.inspect
        async def held(address):
            if not entered.is_set():
                entered.set()
                await release.wait()
            return await original_inspect(address)
        self.adapter.inspect = held
        original_abandon = self.owner.abandon_started
        async def abandon(*args):
            abandoning.set()
            return await original_abandon(*args)
        self.owner.abandon_started = abandon
        inspect_reader, inspect_writer = await asyncio.open_unix_connection(str(self.path))
        self.addCleanup(inspect_writer.close)
        inspection = self.m.Request('inspect', s, str(uuid4()), snapshot.reference)
        inspect_writer.write(inspection.encode()+b'\n')
        await inspect_writer.drain()
        async with asyncio.timeout(1.):
            await entered.wait()
            if loss == 'cancel':
                start_handler.cancel()
            elif loss == 'malformed':
                writer.write(self.m.encode(dict(schema_version=1, ack=str(uuid4())))+b'\n')
                await writer.drain()
            else:
                writer.close()
                await writer.wait_closed()
            await abandoning.wait()
        if foreign:
            self.adapter.units[s] = replace(self.adapter.units[s], invocation_id=uuid4().hex)
        self.adapter.kill_error = kill_error
        if stalled:
            self.owner.timeout = .05
            self.server.timeout = .05
            try:
                async with asyncio.timeout(1.):
                    with self.assertRaisesRegex(self.m.Unknown, 'active|cleanup|unknown'):
                        await self.server.aclose()
                self.assertTrue(self.adapter.units[s].populated)
            finally:
                release.set()
                self.owner.timeout = .5
                await self.owner.aclose()
        else:
            release.set()
            async with asyncio.timeout(1.):
                result = self.m.decode((await inspect_reader.readline()).rstrip(b'\n'))
                if not foreign:
                    self.assertEqual(result['status'], 'ok')
                    inspect_writer.write(self.m.encode(dict(schema_version=1, ack=inspection.request_id))+b'\n')
                    await inspect_writer.drain()
                inspect_writer.close()
                await inspect_writer.wait_closed()
                await self.server.drain()
                if foreign or kill_error:
                    with self.assertRaisesRegex(self.m.Unknown, 'mismatch|unavailable|cleanup|unknown'):
                        await self.server.aclose()
                    # Ошибка shutdown остаётся наблюдаемой, но повторный cleanup
                    # unittest не должен маскировать исходный результат.
                    self.owner.last_error = None
                else:
                    await self.server.aclose()
        writer.close()
        await writer.wait_closed()
        self.assertEqual(sum(c[0] == 'start' for c in self.adapter.calls), 1)
        self.assertEqual(sum(c[0] == 'retire' for c in self.adapter.calls), 0 if foreign else 1)
        self.assertEqual(self.adapter.units[s].populated, foreign or kill_error)
        self.assertFalse(self.owner.active)
        with self.assertRaises(self.m.Refused):
            await self.owner.execute(self.p.node_uid, request)

    async def test_lost_ack_queues_owned_retirement_behind_active_inspection(self):
        # HYPOTHESIS: execute(start) снимает owner до ACK, concurrent inspect
        # занимает active, abandon_started теряет единственный cleanup (#726).
        # SOLUTION: отдельный bounded receipt owner до exact ACK; cleanup
        # ждёт уже начатый inspect, новые effects не допускаются.
        # RESULT: 43 GREEN; actual private systemd lost-ACK overlap retired empty.
        # STATUS: FIXED — see #726
        await self.lost_ack_during_inspection()

    async def test_cancelled_receipt_handler_queues_cleanup_behind_inspection(self):
        await self.lost_ack_during_inspection(loss='cancel')

    async def test_malformed_ack_queues_cleanup_behind_inspection(self):
        await self.lost_ack_during_inspection(loss='malformed')

    async def test_lost_ack_overlap_never_retires_foreign_invocation_and_shutdown_is_loud(self):
        await self.lost_ack_during_inspection(foreign=True)

    async def test_lost_ack_overlap_failed_retirement_cannot_be_successful_shutdown(self):
        await self.lost_ack_during_inspection(kill_error=True)

    async def test_lost_ack_overlap_stuck_inspection_has_bounded_loud_shutdown(self):
        await self.lost_ack_during_inspection(stalled=True)

    async def test_wrong_supervisor_peer_is_rejected_before_request(self):
        from mop.node.host_scope_client import Client
        from mop.node.http_execution import EffectGuard
        async def allow():
            pass
        from _body_fixture import work
        w = work()
        s = self.m.Scope(w.epoch, w.node_id, w.executor_epoch, w.target_id,
                         w.incarnation_id, str(uuid4()))
        client = Client(self.path, node_uid=os.getuid(), supervisor_uid=os.getuid()+1, timeout=.5, work=w)
        with self.assertRaisesRegex(self.m.Refused, 'peer'):
            await client.start(s, EffectGuard(allow, lambda: None))
        self.assertFalse(any(c[0] == 'start' for c in self.adapter.calls))

    async def test_kernel_peer_not_work_json_is_the_server_authority(self):
        self.owner.installed = replace(self.p, node_uid=os.getuid()+10)
        reader, writer = await asyncio.open_unix_connection(str(self.path))
        # Даже без frame сервер уже знает kernel peer и обязан закрыть socket.
        async with asyncio.timeout(1):
            self.assertEqual(await reader.read(4096), b'')
        writer.close()
        await writer.wait_closed()
        self.assertEqual(self.adapter.calls, [])
        self.assertIn('peer', self.server.last_error)

    async def test_slow_or_oversized_frame_is_bounded_and_does_not_call_adapter(self):
        for data in (b'x'*(self.m.FRAME_LIMIT+1)+b'\n', b'{'):
            reader, writer = await asyncio.open_unix_connection(str(self.path))
            writer.write(data)
            await writer.drain()
            async with asyncio.timeout(1):
                await reader.read(self.m.FRAME_LIMIT+1)
            writer.close()
            await writer.wait_closed()
        self.assertEqual(self.adapter.calls, [])


class SystemdTests(unittest.IsolatedAsyncioTestCase):
    @contextmanager
    def directory_adapter(self):
        """Настоящий consumer cwd, fake manager/kernel; не OS acceptance."""
        from unittest.mock import AsyncMock, patch
        from mop.node import host_scope_systemd as systemd
        with tempfile.TemporaryDirectory(prefix='m725-') as directory:
            root, s = Path(directory), scope()
            p = replace(installed(), node_uid=os.getuid()+1, body_uid=os.getuid(),
                        body_gid=os.getgid(), body_root=root/'bodies')
            p.body_root.mkdir(mode=0o755)
            body = p.body(s)
            body.mkdir(mode=0o700)
            cgroup = root/'cgroup'
            cgroup.mkdir()
            (cgroup/'cgroup.kill').write_bytes(b'')
            adapter = object.__new__(systemd.SystemdCgroupAdapter)
            adapter.installed = p
            adapter._runner_identity = (1, 2, 3, 4)
            adapter.boot_id = str(uuid4())
            values = dict(User=str(p.body_uid), Group=str(p.body_gid),
                          InvocationID=uuid4().hex, ControlGroup=s.cgroup)
            adapter._unit = AsyncMock(return_value=(values, str(p.runner),
                                                   (str(p.runner), '--scope', s.unit)))
            adapter._cgroup = lambda address: os.open(cgroup, os.O_RDONLY | os.O_DIRECTORY)
            adapter._members = lambda fd, address: None
            adapter._populated = lambda fd: not (cgroup/'cgroup.kill').read_bytes()
            original_open = systemd.open_directory
            def open_fixture(path, **kwargs):
                if path == p.body_root:
                    # Только immutable ancestor fixture заменён; consumer leaf
                    # проходит настоящий no-follow open/fstat production _body.
                    return original_open(path, owner=os.getuid())
                return original_open(path, **kwargs)
            with patch.object(systemd, 'open_directory', side_effect=open_fixture), \
                    patch.object(systemd, 'installed_file', return_value=adapter._runner_identity), \
                    patch.object(systemd, 'read_kernel', return_value=adapter.boot_id):
                yield adapter, s, body, cgroup

    async def test_same_invocation_starter_exit_is_not_a_containment_identity_change(self):
        # BUG: штатный double-fork между unit snapshots превращается в Unknown.
        # HYPOTHESIS: MainPID/SubState — lifecycle, не exact invocation authority.
        # SOLUTION: только running/PID>0 -> exited/PID=0 при active и полном
        # равенстве остальных fields; те же два чтения, без нового budget/retry.
        # RESULT: RED здесь и в actual barrier fixture: только MainPID/SubState
        # изменились, ExecMainCode=CLD_EXITED (1), ExecMainStatus=0; затем GREEN.
        # STATUS: FIXED — see #741
        with self.directory_adapter() as (adapter, s, body, cgroup):
            before, executable, argv = adapter._unit.return_value
            before = {**before, 'MainPID': '76', 'ActiveState': 'active', 'SubState': 'running'}
            after = {**before, 'MainPID': '0', 'SubState': 'exited'}
            adapter._unit.side_effect = [(before, executable, argv), (after, executable, argv)]
            facts = await adapter.inspect(s)
            self.assertTrue(facts.verify(adapter.installed, s).populated)
            self.assertEqual(facts.invocation_id, before['InvocationID'])
            self.assertEqual(adapter._unit.await_count, 2, 'no inspection retry or added budget')
            self.assertEqual((cgroup/'cgroup.kill').read_bytes(), b'')

    async def test_start_unknown_pins_first_validated_invocation_without_extra_inspection(self):
        from unittest.mock import AsyncMock, Mock
        for failure in ('members', 'invocation', 'no-unit', 'invalid-executable'):
            with self.subTest(failure=failure), self.directory_adapter() as (adapter, s, body, cgroup):
                values, executable, argv = adapter._unit.return_value
                before = (values, executable, argv)
                after = ({**values, 'InvocationID': uuid4().hex}, executable, argv)
                if failure == 'no-unit':
                    before = None
                elif failure == 'invalid-executable':
                    before = (values, '/bin/sh', argv)
                adapter._body = Mock()
                adapter.command = Mock(run=AsyncMock(return_value=b''))
                adapter._unit.side_effect = [None, before, after]
                if failure == 'members':
                    adapter._members = Mock(side_effect=api().Unknown('kernel membership unavailable'))
                with self.assertRaises(api().StartUnknown) as caught:
                    await adapter.start(s)
                reference = caught.exception.reference
                if failure in ('no-unit', 'invalid-executable'):
                    self.assertIsNone(reference, 'unvalidated unit is not cleanup authority')
                else:
                    self.assertEqual(reference, api().Reference(s, values['InvocationID'], adapter.boot_id))
                self.assertEqual(adapter.command.run.await_count, 2, 'one environment read, one start')
                self.assertEqual(adapter._unit.await_count, 3 if failure == 'invocation' else 2)
                self.assertEqual((cgroup/'cgroup.kill').read_bytes(), b'')

    async def test_starter_exit_never_masks_identity_or_unexpected_state_changes(self):
        for change in ({'InvocationID': uuid4().hex}, {'ControlGroup': '/foreign'},
                       {'User': '0'}, {'Group': '0'}, {'ActiveState': 'failed'},
                       {'SubState': 'dead'}, {'MainPID': '77'}):
            with self.subTest(change=change), self.directory_adapter() as (adapter, s, body, cgroup):
                before, executable, argv = adapter._unit.return_value
                before = {**before, 'MainPID': '76', 'ActiveState': 'active', 'SubState': 'running'}
                after = {**before, 'MainPID': '0', 'SubState': 'exited', **change}
                adapter._unit.side_effect = [(before, executable, argv), (after, executable, argv)]
                with self.assertRaises((api().Unknown, api().Refused)):
                    await adapter.inspect(s)
                self.assertEqual(adapter._unit.await_count, 2)
                self.assertEqual((cgroup/'cgroup.kill').read_bytes(), b'')
        for executable_change, argv_change in (('/bin/sh', None), (None, ('/bin/sh',))):
            with self.directory_adapter() as (adapter, s, body, cgroup):
                before, executable, argv = adapter._unit.return_value
                adapter._unit.side_effect = [(before, executable, argv),
                    (before, executable_change or executable, argv_change or argv)]
                with self.assertRaises((api().Unknown, api().Refused)):
                    await adapter.inspect(s)
                self.assertEqual((cgroup/'cgroup.kill').read_bytes(), b'')

    async def test_owned_retirement_ignores_consumer_cwd_mutations(self):
        # HYPOTHESIS: launch admission _body повторно доверяет mutable cwd
        # и отказывает до exact unit/invocation/cgroup cleanup (#725).
        # SOLUTION: cwd admission только перед start; runner/unit/reference
        # и pinned cgroup fences остаются на inspect/retire.
        # RESULT: 37 GREEN; private systemd probe принял chmod/rename/remove.
        # STATUS: FIXED — see #725
        for mutation in ('chmod', 'rename', 'remove', 'symlink'):
            with self.subTest(mutation=mutation), self.directory_adapter() as (adapter, s, body, cgroup):
                reference = (await adapter.inspect(s)).verify(adapter.installed, s).reference
                if mutation == 'chmod':
                    body.chmod(0o755)
                elif mutation == 'rename':
                    body.rename(body.with_name('moved'))
                else:
                    body.rmdir()
                    if mutation == 'symlink':
                        body.symlink_to(cgroup, target_is_directory=True)
                observed = await adapter.inspect(s)
                self.assertEqual(observed.verify(adapter.installed, s).reference, reference)
                empty = await adapter.retire(reference)
                self.assertFalse(empty.verify(adapter.installed, s, reference).populated)
                self.assertEqual((cgroup/'cgroup.kill').read_bytes(), b'1')

    async def test_launch_still_requires_original_private_consumer_directory(self):
        for mutation in ('chmod', 'remove', 'symlink'):
            with self.subTest(mutation=mutation), self.directory_adapter() as (adapter, s, body, cgroup):
                if mutation == 'chmod':
                    body.chmod(0o755)
                else:
                    body.rmdir()
                    if mutation == 'symlink':
                        body.symlink_to(cgroup, target_is_directory=True)
                with self.assertRaises((api().Refused, OSError)):
                    await adapter.start(s)
                adapter._unit.assert_not_called()
                self.assertEqual((cgroup/'cgroup.kill').read_bytes(), b'')

    async def test_mutated_cwd_never_relaxes_executable_or_exact_reference_fences(self):
        from unittest.mock import patch
        from mop.node import host_scope_systemd as systemd
        with self.directory_adapter() as (adapter, s, body, cgroup):
            reference = (await adapter.inspect(s)).verify(adapter.installed, s).reference
            body.chmod(0o755)
            for changes in ({'invocation_id': uuid4().hex}, {'boot_id': str(uuid4())},
                            {'scope': scope()}):
                with self.subTest(changes=changes), self.assertRaises(api().Refused):
                    await adapter.retire(replace(reference, **changes))
                self.assertEqual((cgroup/'cgroup.kill').read_bytes(), b'')
            values, executable, argv = adapter._unit.return_value
            for foreign in ((values, '/bin/sh', argv), (values, executable, ('/bin/sh',)),
                            ({**values, 'ControlGroup': '/foreign'}, executable, argv)):
                adapter._unit.return_value = foreign
                with self.assertRaises(api().Refused):
                    await adapter.retire(reference)
                self.assertEqual((cgroup/'cgroup.kill').read_bytes(), b'')
            adapter._unit.return_value = values, executable, argv
            with patch.object(systemd, 'installed_file', return_value=(9, 9, 9, 9)):
                with self.assertRaisesRegex(api().Refused, 'runner identity'):
                    await adapter.retire(reference)
            self.assertEqual((cgroup/'cgroup.kill').read_bytes(), b'')

    @contextmanager
    def proc_adapter(self, name=b'fixture', *, start=b'12345', status_changes=None):
        """Bounded byte fixtures /proc, без обхода настоящих host processes."""
        from unittest.mock import patch
        from mop.node import host_scope_systemd as systemd
        with tempfile.TemporaryDirectory(prefix='m727-') as directory:
            root, s, p = Path(directory), scope(), installed()
            group, proc = root/'cgroup', root/'123'
            group.mkdir(mode=0o700)
            proc.mkdir(mode=0o700)
            for key, value in {'cgroup.type': b'domain\n', 'cgroup.subtree_control': b'',
                               'cgroup.procs': b'123\n'}.items():
                (group/key).write_bytes(value)
            raw_stat = b'123 ('+name+b') S '+b' '.join([b'0']*18+[start]+[b'0']*30)+b'\n'
            (proc/'stat').write_bytes(raw_stat)
            values = {b'Name': name, b'Uid': b'1102 1102 1102 1102',
                      b'Gid': b'1102 1102 1102 1102', b'Groups': b'1102', b'NoNewPrivs': b'1',
                      **{key: b'0000000000000000' for key in (b'CapInh', b'CapPrm', b'CapEff', b'CapAmb')}}
            values.update(status_changes or {})
            (proc/'status').write_bytes(b''.join(key+b':\t'+value+b'\n' for key, value in values.items()))
            (proc/'cgroup').write_bytes(b'0::'+s.cgroup.encode()+b'\n')
            adapter = object.__new__(systemd.SystemdCgroupAdapter)
            adapter.installed = p
            original = systemd.open_directory
            def open_fixture(path, **kwargs):
                return original(proc if path == Path('/proc/123') else path, **kwargs)
            fd = original(group)
            try:
                with patch.object(systemd, 'open_directory', side_effect=open_fixture):
                    yield adapter, fd, s, proc
            finally:
                os.close(fd)

    async def test_membership_proof_treats_kernel_comm_as_opaque_bytes(self):
        # HYPOTHESIS: ASCII decode всего stat/status делает body-controlled
        # comm authority над retirement; kernel допускает любые non-NUL bytes.
        # SOLUTION: bounded raw reads, opaque comm, ASCII только required
        # fields, numeric PID/starttime и before/after identity comparison.
        # RESULT: 48 GREEN, actual own PR_SET_NAME children и private systemd
        # Unicode/invalid-UTF8 retirement GREEN (до fix probe был RED).
        # STATUS: FIXED — see #727
        for name in ('тело'.encode(), b'\xff\xfe a)b(', b'space ) ( name', b'line\nname'):
            with self.subTest(name=name), self.proc_adapter(name) as (adapter, fd, s, proc):
                adapter._members(fd, s)

    async def test_opaque_comm_never_weakens_uid_gid_groups_caps_or_cgroup_proofs(self):
        cases = [{b'Uid': b'0 0 0 0'}, {b'Gid': b'0 0 0 0'}, {b'Groups': b'1102 0'},
                 {b'NoNewPrivs': b'0'}, {b'Uid': b'\xff 1102 1102 1102'},
                 *({key: b'0000000000000001'} for key in (b'CapInh', b'CapPrm', b'CapEff', b'CapAmb'))]
        for changes in cases:
            with self.subTest(changes=changes), self.proc_adapter(b'\xff ()', status_changes=changes) as (adapter, fd, s, proc):
                with self.assertRaises(api().Refused):
                    adapter._members(fd, s)
        with self.proc_adapter(b'\xff ()') as (adapter, fd, s, proc):
            (proc/'cgroup').write_bytes(b'0::/foreign\n')
            with self.assertRaises(api().Refused):
                adapter._members(fd, s)

    async def test_proc_start_proof_is_numeric_pid_bound_and_stable_before_after(self):
        from unittest.mock import patch
        from mop.node import host_scope_systemd as systemd
        for value in (b'not-a-number', b'-1', str(2**64).encode(), b'\xff'):
            with self.subTest(value=value), self.proc_adapter(b'fixture', start=value) as (adapter, fd, s, proc):
                with self.assertRaises(api().Refused):
                    adapter._members(fd, s)
        with self.proc_adapter(b'fixture') as (adapter, fd, s, proc):
            raw = (proc/'stat').read_bytes()
            (proc/'stat').write_bytes(raw.replace(b'123 (', b'124 (', 1))
            with self.assertRaises(api().Refused):
                adapter._members(fd, s)
        with self.proc_adapter(b'fixture') as (adapter, fd, s, proc):
            original = systemd.read_at
            def change_start(directory, name, **kwargs):
                result = original(directory, name, **kwargs)
                if name == 'cgroup':
                    stat_path = proc/'stat'
                    stat_path.write_bytes(stat_path.read_bytes().replace(b'12345', b'12346'))
                return result
            with patch.object(systemd, 'read_at', side_effect=change_start):
                with self.assertRaisesRegex(api().Unknown, 'changed'):
                    adapter._members(fd, s)

    async def test_proc_byte_parsers_reject_missing_duplicate_nonascii_and_oversized_required_facts(self):
        from mop.node.host_scope_systemd import parse_proc_start, parse_proc_status
        raw = b'123 (\xff )) name) S '+b' '.join([b'0']*18+[b'12345'])
        self.assertEqual(parse_proc_start(raw, 123), 12345)
        for bad in (b'', b'124 (x) S '+b'0 '*20, b'123 (unterminated', raw[:30], raw+b'x'*16384):
            with self.subTest(bad=bad[:30]), self.assertRaises(api().Refused):
                parse_proc_start(bad, 123)
        with self.proc_adapter(b'\xff ()') as (adapter, fd, s, proc):
            status = (proc/'status').read_bytes()
            values = parse_proc_status(status)
            self.assertNotIn('Name', values)
            self.assertEqual(values['Uid'].split(), ['1102']*4)
            for bad in (status.replace(b'Uid:', b'Other:', 1), status+b'Uid:\t0\n',
                        status.replace(b'NoNewPrivs:\t1', b'NoNewPrivs:\t\xff'), status+b'x'*16384):
                with self.assertRaises(api().Refused):
                    parse_proc_status(bad)
            (proc/'stat').write_bytes(b'x'*16385)
            with self.assertRaises(api().Unknown):
                adapter._members(fd, s)

    async def test_actual_owned_child_pr_set_name_supports_utf8_invalid_bytes_and_parentheses(self):
        from mop.node import host_scope_systemd as systemd
        code = ('import ctypes, os, sys; libc=ctypes.CDLL(None, use_errno=True); '
                'name=bytes.fromhex(sys.argv[1]); '
                'result=libc.prctl(15, ctypes.c_char_p(name), 0, 0, 0); '
                'assert result == 0, ctypes.get_errno(); '
                'assert libc.prctl(38, 1, 0, 0, 0) == 0; '
                'print(os.getpid(), flush=True); sys.stdin.buffer.read(1)')
        for name in ('тело'.encode(), b'\xff\xfe a)b(', b'a ) b ( c', b'line\nname'):
            with self.subTest(name=name):
                child = await asyncio.create_subprocess_exec(sys.executable, '-I', '-c', code, name.hex(),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=1024)
                try:
                    async with asyncio.timeout(2.):
                        self.assertEqual(int(await child.stdout.readline()), child.pid)
                    proc = systemd.open_directory(Path('/proc')/str(child.pid))
                    try:
                        before = systemd.parse_proc_start(systemd.read_bytes_at(proc, 'stat'), child.pid)
                        status = systemd.parse_proc_status(systemd.read_bytes_at(proc, 'status'))
                        after = systemd.parse_proc_start(systemd.read_bytes_at(proc, 'stat'), child.pid)
                        self.assertEqual(before, after)
                        self.assertEqual(status['Uid'].split(), [str(os.getuid())]*4)
                        self.assertEqual(status['Gid'].split(), [str(os.getgid())]*4)
                        self.assertEqual(status['NoNewPrivs'], '1')
                        self.assertEqual(Path(f'/proc/{child.pid}/comm').read_bytes(), name+b'\n')
                    finally:
                        os.close(proc)
                finally:
                    child.stdin.close()
                    try:
                        async with asyncio.timeout(2.):
                            await child.communicate()
                    except TimeoutError:
                        if child.returncode is None:
                            child.kill()
                        await child.communicate()
                self.assertEqual(child.returncode, 0)

    async def test_production_command_capture_is_bounded_and_cleans_owned_child(self):
        m = api()
        from mop.node.host_scope_systemd import BoundedCommand
        command = BoundedCommand(timeout=.15, limit=2048)
        with self.assertRaisesRegex(m.Unknown, 'limit|bounded'):
            await command.run(sys.executable, '-I', '-c', 'import os; os.write(1,b"x"*20000)')
        with self.assertRaisesRegex(m.Unknown, 'timeout|deadline'):
            await command.run(sys.executable, '-I', '-c', 'import time; time.sleep(10)')
        await command.aclose()
        self.assertFalse(command.active)

    async def test_reap_never_signals_a_pid_already_reaped_and_available_for_reuse(self):
        api()
        from unittest.mock import patch
        from mop.node.host_scope_systemd import BoundedCommand
        command = BoundedCommand(timeout=1.)
        with patch('os.killpg') as kill:
            self.assertEqual(await command.run(sys.executable, '-I', '-c', 'print("done")'), b'done\n')
            await command.aclose()
        kill.assert_not_called()

    async def test_command_failure_keeps_bounded_diagnostic_without_reflecting_raw_stderr(self):
        api()
        from mop.node.host_scope_systemd import BoundedCommand, CommandFailure
        command = BoundedCommand(timeout=1.)
        with self.assertRaises(CommandFailure) as caught:
            await command.run(sys.executable, '-I', '-c',
                              'import sys; sys.stderr.write("fixture diagnostic"); sys.exit(7)')
        self.assertEqual(caught.exception.stderr, b'fixture diagnostic')
        self.assertNotIn('fixture diagnostic', str(caught.exception))
        await command.aclose()

    # HYPOTHESIS (#740): snapshot node-only теряет общий initial-create protocol.
    # SOLUTION: явный source closure, не весь checkout и не installation inputs.
    # RESULT: RED missing common sources; 49 checks GREEN и actual OS probe accepted.
    # Отдельный startup unit-change unknown остаётся в #741, повтор его не исправляет.
    # STATUS: FIXED — see #740.
    def test_probe_snapshot_imports_current_protocol_without_ambient_package_or_private_inputs(self):
        import shutil
        import subprocess
        from mop.node import host_scope_probe
        snapshot = getattr(host_scope_probe, 'snapshot_sources', None)
        self.assertTrue(callable(snapshot), 'scope probe needs a testable explicit source snapshot')
        modules = ('host_scope.py', 'host_scope_client.py', 'host_scope_supervisor.py',
                   'host_scope_systemd.py', 'http_execution.py')
        helpers = (('_host_scope_runner.py', 'runner.py'), ('_host_scope_entry.py', 'entry.py'),
                   ('_host_scope_acceptance.py', 'acceptance.py'))
        source_files = ['mop/node/'+name for name in modules] + ['mop/common/initialcreate.py']
        source_files += ['tests/'+name for name, _ in helpers]
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix='mop-probe-sources-') as directory:
            source, context = Path(directory)/'source', Path(directory)/'context'
            context.mkdir()
            for relative in source_files:
                target = source/relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root/relative, target)
            (source/'.env').write_text('fixture-only installation sentinel')
            (source/'mop/node/unselected.py').write_text('raise AssertionError("unselected source")')
            snapshot(source, context)
            expected = {'package/mop/node/'+name for name in modules}
            expected |= {'package/mop/common/initialcreate.py', 'package/mop/__init__.py',
                         'package/mop/node/__init__.py', 'package/mop/common/__init__.py', 'Dockerfile'}
            expected |= {destination for _, destination in helpers}
            self.assertEqual({str(path.relative_to(context)) for path in context.rglob('*') if path.is_file()}, expected)
            script = '''import sys
sys.path.insert(0, sys.argv[1])
from mop.node import host_scope, host_scope_client, host_scope_supervisor, host_scope_systemd, http_execution
assert 'prepare_body' in http_execution.ACTIONS
assert 'mop.common.config' not in sys.modules
'''
            result = subprocess.run([sys.executable, '-ISB', '-c', script, str(context/'package')],
                cwd=directory, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertFalse(list(context.rglob('__pycache__')))

    async def test_fixture_plan_is_new_private_and_never_mounts_host_cgroup_or_socket(self):
        api()
        from mop.node.host_scope_probe import fixture_args, verify_fixture_config
        identifier = str(uuid4())
        args = fixture_args(identifier)
        joined = ' '.join(args)
        for item in ('--privileged', '--cgroupns=private', '--network=none'):
            self.assertIn(item, args)
        # Docker не принимает --pid=private: default private проверяется также
        # по actual HostConfig до start, а PID 1 внутри проверяет entry script.
        self.assertFalse(any(arg.startswith('--pid=') for arg in args))
        config = dict(Privileged=True, PidMode='', CgroupnsMode='private', NetworkMode='none',
                      Binds=None, Mounts=None, VolumesFrom=None, CgroupParent='')
        verify_fixture_config(config)
        for changes in ({'PidMode': 'host'}, {'CgroupnsMode': 'host'}, {'NetworkMode': 'host'},
                        {'Binds': ['/sys/fs/cgroup:/sys/fs/cgroup']},
                        {'Mounts': [{'Source': '/var/run/docker.sock'}]}, {'Privileged': False}):
            with self.assertRaises(api().Refused):
                verify_fixture_config({**config, **changes})
        for item in ('--volume', '-v', '--mount', '--network=host', '--pid=host', '--cgroupns=host',
                     '/var/run/docker.sock', '/sys/fs/cgroup:/sys/fs/cgroup'):
            self.assertNotIn(item, joined)
        with self.assertRaises(api().Refused):
            fixture_args('../foreign')

    async def test_fixed_systemd_launch_has_no_caller_argv_env_or_delegation(self):
        api()
        from mop.node.host_scope_systemd import launch_argv
        s, p = scope(), installed()
        args = launch_argv(p, s)
        joined = ' '.join(args)
        for flag in ('NoNewPrivileges=yes', 'Delegate=no', 'ProtectControlGroups=yes',
                     'CapabilityBoundingSet=', 'KillMode=control-group', 'Restart=no',
                     'RestrictSUIDSGID=yes', 'RestrictNamespaces=yes'):
            self.assertIn(flag, joined)
        self.assertEqual(args[-3:], (str(p.runner), '--scope', s.unit))
        self.assertIn('User=' + str(p.body_uid), joined)
        self.assertIn('Group=' + str(p.body_gid), joined)
        self.assertIn(str(p.node_private), joined)
        self.assertNotIn('TOKEN', joined)
        self.assertNotIn('--scope-probe-shell', joined)

    async def test_exec_prefix_flags_are_not_hidden_by_legacy_execstart_property(self):
        api()
        from mop.node.host_scope_systemd import parse_exec_ex
        s, p = scope(), installed()
        row = [str(p.runner), [str(p.runner), '--scope', s.unit], [], 0, 0, 0, 0, 7, 0, 0]
        self.assertEqual(parse_exec_ex(json.dumps({'type': 'a(sasasttttuii)', 'data': [row]}).encode()),
                         (str(p.runner), (str(p.runner), '--scope', s.unit)))
        for flags in (['privileged'], ['no-setuid'], ['ignore-failure'], None, False):
            with self.assertRaises(api().Refused):
                parse_exec_ex(json.dumps({'type': 'a(sasasttttuii)',
                                         'data': [[*row[:2], flags, *row[3:]]]}).encode())

    async def test_cleanup_failure_cannot_disappear_from_the_command_owner(self):
        api()
        from mop.node.host_scope_systemd import BoundedCommand
        command = BoundedCommand()
        async def failed():
            raise PermissionError('owned child reap denied')
        task = asyncio.create_task(failed())
        await asyncio.wait({task})
        command._done(task)
        with self.assertRaisesRegex(api().Unknown, 'cleanup'):
            await command.aclose()

    async def test_no_follow_bounded_kernel_reads_refuse_symlinks_and_fifos(self):
        api()
        from mop.node.host_scope_systemd import read_at, open_directory
        with tempfile.TemporaryDirectory(prefix='m719-fd-') as directory:
            root = Path(directory)
            (root/'fact').write_bytes(b'x'*2048)
            (root/'link').symlink_to(root/'fact')
            os.mkfifo(root/'fifo')
            fd = open_directory(root, owner=os.getuid(), private=True)
            try:
                with self.assertRaises(api().Unknown):
                    read_at(fd, 'fact', limit=1024)
                with self.assertRaises(OSError):
                    read_at(fd, 'link')
                with self.assertRaises(api().Refused):
                    read_at(fd, 'fifo')
            finally:
                os.close(fd)

    async def test_empty_command_arrays_require_structured_kernel_manager_evidence(self):
        api()
        from mop.node.host_scope_systemd import parse_empty
        parse_empty(b'{"type":"a(sasbttttuii)","data":[]}', 'a(sasbttttuii)')
        for value in ({'type': 'a(sasbttttuii)', 'data': None},
                      {'type': 'a(sasbttttuii)', 'data': [['/bin/sh']]},
                      {'type': 's', 'data': ''}, {}, {'type': 'a(sasbttttuii)'}):
            with self.assertRaises(api().Refused):
                parse_empty(json.dumps(value).encode(), 'a(sasbttttuii)')

    async def test_actual_systemctl_empty_properties_must_be_explicitly_requested(self):
        api()
        from mop.node.host_scope_systemd import SystemdCgroupAdapter, properties, EXTRA_PROPERTIES, STRUCTURED_EMPTY
        p, s = installed(), scope()
        values = dict.fromkeys(EXTRA_PROPERTIES, '')
        values.update(properties(p, s))
        values.update(Id=s.unit, LoadState='loaded', Transient='yes', DynamicUser='no',
                      PrivateUsers='no', FragmentPath='/run/systemd/transient/'+s.unit,
                      ControlGroup=s.cgroup, MainPID='7', InvocationID=uuid4().hex,
                      ActiveState='active', SubState='running')
        class Command:
            async def run(self, executable, *args):
                if '--property=LoadState' in args:
                    return b'LoadState=loaded\n'
                if 'get-property' in args and args[-1] in STRUCTURED_EMPTY:
                    return json.dumps({'type': STRUCTURED_EMPTY[args[-1]], 'data': []}).encode()
                if 'get-property' in args and args[-1] == 'ExecStartEx':
                    return json.dumps({'type': 'a(sasasttttuii)', 'data': [[str(p.runner),
                        [str(p.runner), '--scope', s.unit], [], 0, 0, 0, 0, 7, 0, 0]]}).encode()
                if 'get-property' in args:
                    return json.dumps({'type': 'a(sasbttttuii)', 'data': [[str(p.runner),
                        [str(p.runner), '--scope', s.unit], False, 0, 0, 0, 0, 7, 0, 0]]}).encode()
                return ''.join(k+'='+v+'\n' for k, v in values.items()
                               if v or '--all' in args).encode()
        adapter = object.__new__(SystemdCgroupAdapter)
        adapter.installed, adapter.command = p, Command()
        result = await adapter._unit(s)
        self.assertEqual(result[1:], (str(p.runner), (str(p.runner), '--scope', s.unit)))

    async def test_missing_unit_does_not_need_nonexistent_service_properties(self):
        api()
        from mop.node.host_scope_systemd import SystemdCgroupAdapter
        class Command:
            async def run(self, *args):
                return b'LoadState=not-found\n'
        adapter = object.__new__(SystemdCgroupAdapter)
        adapter.installed, adapter.command = installed(), Command()
        self.assertIsNone(await adapter._unit(scope()))

    async def test_unit_property_parser_rejects_duplicates_unrequested_properties_and_exec_substitution(self):
        m = api()
        from mop.node.host_scope_systemd import parse_properties, parse_exec
        with self.assertRaisesRegex(m.Refused, 'Group'):
            parse_properties(b'User=1102\n', {'User', 'Group'})
        with self.assertRaises(m.Refused):
            parse_properties(b'User=1102\nUser=0\n', {'User'})
        with self.assertRaises(m.Refused):
            parse_properties(b'User=1102\nEnvironment=TOKEN=x\n', {'User'})
        s, p = scope(), installed()
        value = {'type': 'a(sasbttttuii)', 'data': [[str(p.runner),
                 [str(p.runner), '--scope', s.unit], False, 0, 0, 0, 0, 1, 0, 0]]}
        self.assertEqual(parse_exec(json.dumps(value).encode()),
                         (str(p.runner), (str(p.runner), '--scope', s.unit)))
        value['data'].append(value['data'][0])
        with self.assertRaises(m.Refused):
            parse_exec(json.dumps(value).encode())


if __name__ == '__main__':
    unittest.main()
