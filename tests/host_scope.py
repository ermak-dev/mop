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

    async def test_start_lost_os_ack_attempts_owned_cleanup_and_stays_unknown(self):
        original = self.adapter.start
        async def lost(address):
            await original(address)
            raise self.m.Unknown('launch ACK lost')
        self.adapter.start = lost
        with self.assertRaisesRegex(self.m.Unknown, 'ACK lost'):
            await self.start()
        self.assertFalse(self.adapter.units[self.s].populated)
        with self.assertRaises(self.m.Refused):
            await self.start()

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
