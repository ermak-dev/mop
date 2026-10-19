#!/usr/bin/env python3
"""HYPOTHESIS: planned phases не имеют admitted physical HOST broker.
SPEC: три finite phases, actual Admission, exact retained input/reference,
одноразовые connection checkpoints и installed content/resource receipts.
SOLUTION: root-owned tasks и immutable body-UID broker, без native/ready.
RESULT: RED для отсутствующих контрактов и cleanup/recovery дефектов;
GREEN через mop dev test host_phases и actual owned planned-phase-probe.
STATUS: FIXED — see #752
"""
import asyncio
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import importlib.util
import inspect
from pathlib import Path
import unittest
from uuid import uuid4

import _lib  # noqa: F401
import hermetic  # noqa: F401
from _host_inputs_data import selected
from mop.node.http_execution import EffectGuard, Refused


def api(name='host_phases'):
    assert importlib.util.find_spec('mop.node.'+name) is not None, \
        'actual planned phase '+name+' is missing (#752)'
    return importlib.import_module('mop.node.'+name)


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_passes_the_actual_claim_admission_to_effect_guard(self):
        from mop.node.http_execution import Runtime
        a, _, _ = selected()
        calls = []
        class Client:
            async def claim(self, work, request):
                self.assert_work = work
                return a
            async def recheck(self, admission):
                calls.append(admission)
            async def result(self, admission, outcome, evidence):
                calls.append((admission, outcome, evidence))
        class Executor:
            async def execute(self, work, guard):
                self_guard = getattr(guard, 'admission', None)
                assert self_guard is a, 'Runtime lost the exact Admission before the physical phase boundary'
                await guard()
                guard.check()
                return 'succeeded', 'body-prepared'
        r = Runtime(Client(), Executor(), node_id=a.work.node_id, epoch=a.work.epoch,
                    executor_epoch=a.work.executor_epoch)
        r._ready = True
        await r._execute(a.work)
        self.assertIsNone(r.last_error)
        self.assertIs(calls[0], a)
        self.assertEqual(calls[-1], (a, 'succeeded', 'body-prepared'))

    async def test_legacy_guards_still_work_but_cannot_admit_a_planned_phase(self):
        called = []
        async def authorize(): called.append(True)
        g = EffectGuard(authorize, lambda: None)
        await g()
        self.assertEqual(called, [True])
        m = api()
        with self.assertRaisesRegex(Refused, 'Admission'):
            m.admitted(g, selected()[0].work)


class CommitTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_ack_binds_committed_sql_identity_to_retained_phase_receipt(self):
        from types import SimpleNamespace
        from mop.node.host_phase_supervisor import PhaseOwner
        from mop.node.host_scope import encode, decode
        a, scope, _ = selected()
        receipt = str(uuid4())
        item = SimpleNamespace(submission=SimpleNamespace(admission=a, descriptor=SimpleNamespace(scope=scope)),
            status='succeeded', phase_receipt=receipt, acknowledged=False)
        body = SimpleNamespace(completed=[])
        owner = PhaseOwner.__new__(PhaseOwner)
        owner._attempt = lambda s, attempt: item
        owner.bodies = {scope: body}
        owner.status = lambda observed: {}
        class Writer:
            def __init__(self): self.frames = []
            def write(self, raw): self.frames.append(decode(raw.strip()))
            async def drain(self): pass
        for identity, expected in ((str(uuid4()), 'refused'), (receipt, 'ok')):
            reader = asyncio.StreamReader()
            reader.feed_data(encode(dict(schema_version=1, action='phase_response_ack'))+b'\n')
            reader.feed_eof()
            writer = Writer()
            request = dict(schema_version=1, action='phase_ack', scope=scope.wire(), attempt_id=a.attempt_id,
                receipt=receipt, committed=dict(receipt_id=identity, attempt_id=a.attempt_id,
                    revision=str(a.revision+1), outcome='succeeded', evidence='body-prepared'))
            await owner.handle_planned(reader, writer, encode(request))
            self.assertEqual(writer.frames[0]['status'], expected)
        self.assertEqual(body.completed, ['prepare_body'])

    async def test_lost_result_ack_reuses_retained_root_receipt_without_reexecuting_effect(self):
        from mop.node.http_execution import MachineClient, Runtime, Unavailable
        a, _, _ = selected()
        identity = str(uuid4())
        calls = []
        class Transport:
            async def request(self, method, path, payload=None, params=None):
                calls.append(dict(payload))
                if len(calls) == 1:
                    raise Unavailable('fixture committed result ACK lost')
                return dict(schema_version=1, **{k: payload[k] for k in ('receipt_id', 'attempt_id', 'outcome', 'evidence')}, revision='2')
        client = MachineClient(Transport())
        with self.assertRaises(Unavailable):
            await client.result(a, 'succeeded', 'body-prepared', receipt_id=identity)
        receipt = await client.result(a, 'succeeded', 'body-prepared', receipt_id=identity)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(receipt.receipt_id, identity)
        class Client:
            async def claim(self, *args): return a
            async def recheck(self, admission): pass
            async def result(self, admission, outcome, evidence, *, receipt_id):
                self_identity.append(receipt_id)
                return receipt
        class Executor:
            async def execute(self, work, guard): return 'succeeded', 'body-prepared'
            def result_identity(self, admission):
                assert admission is a
                return identity
            async def acknowledge(self, committed): pass
        self_identity = []
        r = Runtime(Client(), Executor(), node_id=a.work.node_id, epoch=a.work.epoch, executor_epoch=a.work.executor_epoch)
        r._ready = True
        await r._execute(a.work)
        self.assertIsNone(r.last_error)
        self.assertEqual(self_identity, [identity])

    async def test_runtime_acknowledges_root_only_after_exact_committed_machine_result(self):
        from mop.node.http_execution import MachineClient, Runtime
        a, _, _ = selected()
        trace, committed = [], []
        class Transport:
            async def request(self, method, path, payload=None, params=None):
                assert path == 'work/result'
                trace.append('SQL-commit')
                return dict(schema_version=1, **{k: payload[k] for k in ('receipt_id', 'attempt_id', 'outcome', 'evidence')}, revision='2')
        client = MachineClient(Transport())
        receipt = await client.result(a, 'succeeded', 'body-prepared')
        self.assertIsNotNone(receipt, 'MachineClient discards the exact committed result needed for root phase_ack')
        self.assertEqual(receipt.admission, a)
        self.assertEqual(receipt.revision, 2)
        class Client:
            async def claim(self, *args): return a
            async def recheck(self, admission): pass
            async def result(self, *args):
                trace.append('SQL-commit')
                return receipt
        class Executor:
            async def execute(self, work, guard):
                trace.append('physical-effect')
                return 'succeeded', 'body-prepared'
            async def acknowledge(self, result):
                trace.append('root-phase-ack')
                committed.append(result)
        r = Runtime(Client(), Executor(), node_id=a.work.node_id, epoch=a.work.epoch, executor_epoch=a.work.executor_epoch)
        r._ready = True
        await r._execute(a.work)
        self.assertIsNone(r.last_error)
        self.assertIs(committed[0], receipt)
        self.assertEqual(trace[-3:], ['physical-effect', 'SQL-commit', 'root-phase-ack'])


class CheckpointDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_diagnostic_retains_only_last_approved_compiled_effect(self):
        import asyncio
        import time
        from types import SimpleNamespace
        from mop.node.host_phase_supervisor import PhaseOwner
        item = SimpleNamespace(deadline=time.monotonic()+5, pending=None,
            submission=SimpleNamespace(admission=SimpleNamespace(work=SimpleNamespace(action='prepare_body'))))
        owner = PhaseOwner.__new__(PhaseOwner)
        task = asyncio.create_task(owner.checkpoint(item, 'git-fetch', 5))
        await asyncio.sleep(0)
        checkpoint = item.pending
        connection = object()
        nonce = checkpoint.challenge(connection)
        checkpoint.approve(connection, nonce)
        item.approved.set()
        await task
        self.assertIsNone(item.pending)
        self.assertEqual(getattr(item, 'last_effect', None), 'git-fetch')


class EarlyStartCleanupTests(unittest.IsolatedAsyncioTestCase):
    # BUG #758: отмена prepare во время physical start теряет cleanup owner.
    # HYPOTHESIS: phase override ломает _abandon, а phase cleanup не ждёт start.
    # SOLUTION: разные cleanup методы; phase owner joins original action и empty proof.
    # RESULT: 38 phase checks GREEN; actual 3.12/PG17 cancel+timeout — exact cleanup,
    # один start/retire, repeated cancel не бросает retained task (125 rechecks).
    # STATUS: FIXED — see #758
    def owner(self):
        from types import SimpleNamespace
        from _host_scope_fixture import Adapter, installed, scope
        from mop.node.host_phase_supervisor import Body, PhaseOwner
        from mop.node.host_scope_supervisor import Supervisor
        adapter = Adapter()
        adapter.release = asyncio.Event()
        owner = PhaseOwner.__new__(PhaseOwner)
        Supervisor.__init__(owner, installed(), adapter)
        owner.config = SimpleNamespace(control=lambda scope: Path('/unused-phase-control'))
        body = Body(scope(), None)
        return owner, adapter, body

    async def test_abandoned_physical_start_keeps_inherited_cleanup_contract(self):
        from mop.node.host_scope import Request
        owner, adapter, body = self.owner()
        task = asyncio.create_task(owner.execute(owner.installed.node_uid, Request('start', body.scope, str(uuid4()))))
        await adapter.entered.wait()
        action = owner.active[body.scope]
        task.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsNotNone(action.cleanup, 'inherited physical cleanup was never scheduled')
        finally:
            adapter.release.set()
            await asyncio.gather(task, action.task, *([action.cleanup] if action.cleanup else []), return_exceptions=True)
        self.assertFalse(adapter.units[body.scope].populated)
        self.assertNotIn(body.scope, owner.active)
        retired = action.cleanup.result()
        self.assertIsNotNone(retired, 'physical cleanup discarded its exact retirement outcome')
        self.assertEqual(retired.reference, owner._owned[body.scope])
        self.assertFalse(retired.populated)

    async def test_phase_artifacts_wait_for_original_start_and_exact_reference(self):
        from mop.node.host_scope import Request
        owner, adapter, body = self.owner()
        task = asyncio.create_task(owner.execute(owner.installed.node_uid, Request('start', body.scope, str(uuid4()))))
        await adapter.entered.wait()
        cleanup = asyncio.create_task(owner._cleanup_owned(body))
        try:
            await asyncio.sleep(0)
            self.assertFalse(cleanup.done(), 'phase cleanup failed before joining the original retained start')
            self.assertIsNone(body.reference)
            self.assertEqual([name for name, _ in adapter.calls].count('start'), 1)
            adapter.release.set()
            snapshot = await task
            await cleanup
            self.assertEqual(body.reference, snapshot.reference)
            self.assertFalse(adapter.units[body.scope].populated)
            self.assertEqual([name for name, _ in adapter.calls].count('start'), 1)
        finally:
            adapter.release.set()
            await asyncio.gather(task, cleanup, return_exceptions=True)

    async def test_uncertain_original_start_never_adopts_a_later_matching_unit(self):
        from mop.node.host_scope import Request, Unknown
        owner, adapter, body = self.owner()
        original = adapter.start
        async def uncertain(scope):
            await original(scope)
            raise Unknown('original physical outcome has no reference')
        adapter.start = uncertain
        task = asyncio.create_task(owner.execute(owner.installed.node_uid, Request('start', body.scope, str(uuid4()))))
        await adapter.entered.wait()
        cleanup = asyncio.create_task(owner._cleanup_owned(body))
        adapter.release.set()
        with self.assertRaises(Unknown): await task
        with self.assertRaises(Unknown): await cleanup
        self.assertIsNone(body.reference)
        self.assertNotIn(body.scope, owner._owned)
        self.assertEqual([name for name, _ in adapter.calls].count('retire'), 0)
        with self.assertRaisesRegex(Refused, 'replay|uncertain'):
            await owner.execute(owner.installed.node_uid, Request('start', body.scope, str(uuid4())))


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_cleanup_still_joins_every_owned_body_and_closes_private_store(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from mop.node.host_phase_supervisor import PhaseOwner
        from mop.node.host_scope_supervisor import Supervisor
        from mop.node.host_scope import Unknown
        owner = PhaseOwner.__new__(PhaseOwner)
        owner.attempts, owner.bodies, owner._body_handlers = {}, {'first': 'first', 'second': 'second'}, set()
        calls = []
        owner.store = SimpleNamespace(close=lambda: calls.append('store-close'))
        async def cleanup(body):
            calls.append(body)
            if body == 'first': raise Unknown('first exact cleanup unknown')
        async def super_close(self): calls.append('super-close')
        owner._cleanup_phase = cleanup
        with patch.object(Supervisor, 'aclose', super_close), self.assertRaisesRegex(Unknown, 'first exact cleanup unknown'):
            await owner.aclose()
        self.assertEqual(calls, ['first', 'second', 'store-close', 'super-close'])


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_root_status_projects_original_admission_without_claim_or_new_effect(self):
        from types import SimpleNamespace
        from mop.node.host_inputs import Descriptor, admission_wire
        from mop.node.host_phases import Projection, Submission
        from mop.node.host_phase_supervisor import Attempt, PhaseOwner
        a, scope, delivery = selected()
        item = Attempt(Submission(a, Descriptor(scope, a.attempt_id, 'c'*64), Projection.from_delivery(delivery)), None, 0.)
        owner = PhaseOwner.__new__(PhaseOwner)
        owner.bodies = {scope: SimpleNamespace(reference=None, projection=item.submission.projection)}
        owner.manifest = SimpleNamespace(content_digest='d'*64, project=lambda p: SimpleNamespace(wire=lambda: {}))
        self.assertEqual(owner.status(item)['admission'], admission_wire(a))

    async def test_resume_drives_only_retained_original_checkpoint_and_never_new_submission(self):
        from types import SimpleNamespace
        from mop.node.host_inputs import admission_wire
        from mop.node.host_phase_client import PlannedExecutor
        from mop.node.host_scope import Reference
        a, scope, _ = selected()
        receipt = str(uuid4())
        reference = Reference(scope, uuid4().hex, str(uuid4()))
        requests, checks = [], []
        async def authorize(): checks.append(a)
        guard = EffectGuard(authorize, lambda: None, a)
        executor = PlannedExecutor.__new__(PlannedExecutor)
        executor._owned, executor._references = {}, {}
        async def exchange(request, *, guard=None):
            requests.append(request['action'])
            if request['action'] == 'phase_checkpoint':
                self.assertIs(guard.admission, a)
                await guard()
                return dict(status='succeeded', admission=admission_wire(a), reference=reference.wire(), receipt=receipt)
            return dict(status='accepted', admission=admission_wire(a), checkpoint=dict(effect='workspace', sequence=0))
        executor.exchange = exchange
        outcome = await executor.resume(a.work, guard)
        self.assertEqual(outcome, ('succeeded', 'body-prepared'))
        self.assertEqual(requests, ['phase_status', 'phase_checkpoint'])
        self.assertEqual(checks, [a])
        self.assertEqual(executor.result_identity(a), receipt)
        self.assertIs(executor._owned[a.attempt_id].admission, a)
        from mop.node.http_execution import CommittedResult
        with self.assertRaises(Refused):
            await executor.acknowledge(CommittedResult(a, str(uuid4()), 2, 'succeeded', 'body-prepared'))
        self.assertEqual(requests, ['phase_status', 'phase_checkpoint'])
        with self.assertRaises(Refused):
            await executor.resume(a.work, guard)

    async def test_completed_recovery_retries_same_sql_receipt_without_claim_or_new_body_effect(self):
        from mop.node.host_inputs import admission_wire
        from mop.node.host_phase_client import PlannedExecutor
        from mop.node.host_scope import Reference
        from mop.node.http_execution import CommittedResult
        a, scope, _ = selected()
        receipt = str(uuid4())
        reference = Reference(scope, uuid4().hex, str(uuid4()))
        requests, results = [], []
        executor = PlannedExecutor.__new__(PlannedExecutor)
        executor._owned, executor._references = {}, {}
        class Client:
            async def result(self, admission, outcome, evidence, *, receipt_id):
                self_admission.append(admission)
                results.append(receipt_id)
                return CommittedResult(admission, receipt_id, 2, outcome, evidence)
        self_admission = []
        executor.client = Client()
        async def exchange(request, *, guard=None):
            self.assertIsNone(guard)
            requests.append(request['action'])
            return dict(status='succeeded', acknowledged=request['action']=='phase_ack',
                admission=admission_wire(a), reference=reference.wire(), receipt=receipt)
        executor.exchange = exchange
        await executor.recover_result(a)
        self.assertEqual(requests, ['phase_status', 'phase_ack'])
        self.assertEqual(results, [receipt])
        self.assertIs(self_admission[0], a)

    async def test_only_observed_committed_owner_restores_reference_without_claim_or_submission(self):
        from mop.node.host_phase_client import PlannedExecutor
        from mop.node.host_scope import Reference
        _, scope, _ = selected()
        ref = Reference(scope, uuid4().hex, str(uuid4()))
        executor = PlannedExecutor.__new__(PlannedExecutor)
        executor._references = {}
        requests = []
        phase = dict(status='succeeded', acknowledged=True, reference=ref.wire())
        async def exchange(request):
            requests.append(request)
            return phase
        executor.exchange = exchange
        await executor.observe(scope, str(uuid4()))
        self.assertEqual(executor._references.get(scope), ref,
            'frontend replacement cannot continue the exact acknowledged root owner')
        self.assertEqual([r['action'] for r in requests], ['phase_status'])
        executor._references.clear()
        for status, ack in (('accepted', False), ('succeeded', False), ('unknown', True)):
            phase.update(status=status, acknowledged=ack)
            await executor.observe(scope, str(uuid4()))
            self.assertNotIn(scope, executor._references)
        phase.update(status='succeeded', acknowledged=True,
            reference=replace(ref, scope=replace(scope, launch_id=str(uuid4()))).wire())
        with self.assertRaises(Refused):
            await executor.observe(scope, str(uuid4()))


class NodeCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_root_challenge_cannot_spend_actual_admission_or_send_approval(self):
        from types import SimpleNamespace
        from mop.node.host_backend_client import _Session
        from mop.node.host_scope import encode, decode
        a, scope, _ = selected()
        valid = dict(schema_version=1, status='challenge', challenge=str(uuid4()),
            effect='workspace', sequence=0, attempt_id=a.attempt_id)
        for changed in ({'schema_version': True}, {'schema_version': 2},
                        {'challenge': 'not-a-canonical-nonce'}, {'challenge': None}):
            with self.subTest(changed=changed):
                reader = asyncio.StreamReader()
                reader.feed_data(encode({**valid, **changed})+b'\n')
                reader.feed_data(encode(dict(schema_version=1, status='ok', phase={}, error=None))+b'\n')
                reader.feed_eof()
                frames, checked = [], []
                class Writer:
                    def write(self, raw): frames.append(decode(raw.strip()))
                    async def drain(self): pass
                async def authorize(): checked.append(a)
                session = _Session(SimpleNamespace(timeout=5.), reader, Writer(), None, None)
                session.check = lambda proof: None
                guard = EffectGuard(authorize, lambda: None, a)
                with self.assertRaises(Refused):
                    await session.planned(dict(schema_version=1, action='phase_checkpoint',
                        scope=scope.wire(), attempt_id=a.attempt_id), guard=guard)
                self.assertEqual(checked, [])
                self.assertEqual(len(frames), 1, 'invalid challenge sent a checkpoint response')


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.m = api()
        self.a, self.s, self.v = selected()

    def test_finite_frames_never_accept_bool_as_schema_or_checkpoint_sequence(self):
        self.assertTrue(hasattr(self.m, 'require_frame'), 'finite frame equality accepts bool as an integer')
        expected = dict(schema_version=1, sequence=0, action='body_checkpoint', effect='bootstrap-input')
        self.m.require_frame(dict(expected), expected)
        for value in ({**expected, 'schema_version': True}, {**expected, 'sequence': False},
                      {**expected, 'extra': 'callback'}, {**expected, 'sequence': 0.0}):
            with self.assertRaises(Refused): self.m.require_frame(value, expected)

    def test_projection_is_exact_and_contains_no_consumer_bytes_or_physical_uuid_checksum(self):
        p = self.m.Projection.from_delivery(self.v)
        self.assertEqual(p.image_revision, self.v['image_revision'])
        self.assertEqual(p.image_policy, self.v['image_policy'])
        self.assertEqual(p.source, self.v['selection']['source'])
        self.assertNotIn('consumers', p.wire())
        self.assertNotIn(self.v['consumers'][0]['value'], repr(p))
        self.assertEqual(self.m.Projection.parse(p.wire()), p)
        with self.assertRaises(Refused):
            self.m.Projection.parse({**p.wire(), 'artifact_hash': p.image_digest})

    def test_submit_requires_exact_admission_descriptor_and_no_previous_prepare_reference(self):
        from mop.node.host_inputs import pack
        from mop.node.host_scope import Reference
        d = pack(self.a, self.s, self.v).descriptor
        p = self.m.Projection.from_delivery(self.v)
        request = self.m.Submission(self.a, d, p)
        self.assertEqual(self.m.Submission.decode(request.encode()), request)
        ref = Reference(self.s, uuid4().hex, str(uuid4()))
        with self.assertRaises(Refused): self.m.Submission(self.a, d, p, ref)
        with self.assertRaises(Refused): self.m.Submission(self.a, replace(d, attempt_id=str(uuid4())), p)
        with self.assertRaises(Refused): self.m.Submission(self.a, replace(d, scope=replace(self.s, launch_id=str(uuid4()))), p)
        for extra in ({'path': '/root/input'}, {'argv': ['sh']}, {'env': {}}, {'unit_properties': {}},
                      {'callback': 'recheck'}, {'cleanup_descriptor': d.wire()}):
            from mop.node.host_scope import decode, encode
            with self.assertRaises(Refused):
                self.m.Submission.decode(encode({**decode(request.encode()), **extra}))

    def test_bootstrap_and_identity_require_original_physical_reference(self):
        from mop.node.host_inputs import Descriptor
        from mop.node.host_scope import Reference
        ref = Reference(self.s, uuid4().hex, str(uuid4()))
        for action in ('bootstrap_body', 'initial_identity'):
            a = replace(self.a, work=replace(self.a.work, action=action))
            d = Descriptor(self.s, a.attempt_id, 'a'*64)
            p = self.m.Projection.from_delivery(self.v)
            with self.assertRaises(Refused): self.m.Submission(a, d, p)
            self.m.Submission(a, d, p, ref)
            with self.assertRaises(Refused):
                self.m.Submission(a, d, p, replace(ref, scope=replace(self.s, node_id=str(uuid4()))))
        a = replace(self.a, work=replace(self.a.work, action='native_exec'))
        with self.assertRaises(Refused):
            self.m.Submission(a, Descriptor(self.s, a.attempt_id, 'a'*64), self.m.Projection.from_delivery(self.v), ref)

    def test_cutoff_keeps_original_nonrenewable_cleanup_reserve(self):
        effect, cutoff = self.m.cutoff(self.a, monotonic=100.)
        remaining = (self.a.lease_until-datetime.now(timezone.utc)).total_seconds()
        self.assertLessEqual(effect, 60.)
        self.assertAlmostEqual(cutoff-100., remaining-20., delta=.2)
        for seconds in (-1, 0, 19):
            with self.assertRaises(Refused):
                self.m.cutoff(replace(self.a, lease_until=datetime.now(timezone.utc)+timedelta(seconds=seconds)), monotonic=100.)

    def test_full_native_guard_remains_closed_for_nine_actions(self):
        from mop.node.native_startup import NativeBindings
        from mop.node.http_execution import ACTIONS, Unavailable
        self.assertEqual(len(ACTIONS), 9)
        with self.assertRaises(Unavailable): NativeBindings(object())


class SourceTests(unittest.TestCase):
    def test_fixed_https_and_ssh_formats_bind_exact_origin_without_ambient_authority(self):
        m = api('host_phase_source')
        https = dict(format='git-https-basic-v1', origin='https://git.example.invalid:8443/team/repo.git',
                     username='fixture-user', password='private-fixture-password')
        source = m.SourceCredential.parse(https, https['origin'])
        self.assertNotIn(https['password'], repr(source))
        import base64
        import struct
        def field(raw): return struct.pack('>I', len(raw))+raw
        public = field(b'ssh-ed25519')+field(b'x'*32)
        private = b'12341234'+field(b'ssh-ed25519')+field(b'x'*32)+field(b'y'*32+b'x'*32)+field(b'')
        private += bytes(range(1, 9-len(private)%8))
        key = b'openssh-key-v1\x00'+field(b'none')+field(b'none')+field(b'')+struct.pack('>I', 1)+field(public)+field(private)
        ssh = dict(format='git-ssh-key-v1', origin='ssh://git@git.example.invalid:2222/team/repo.git',
                   private_key='-----BEGIN OPENSSH PRIVATE KEY-----\n'+base64.b64encode(key).decode()+'\n-----END OPENSSH PRIVATE KEY-----\n',
                   known_hosts='[git.example.invalid]:2222 ssh-ed25519 '+base64.b64encode(public).decode()+'\n')
        self.assertEqual(m.SourceCredential.parse(ssh, ssh['origin']).port, 2222)
        for item in ({**https, 'format': 'unknown'}, {**https, 'helper': '/tmp/tool'},
                     {**https, 'origin': 'https://other.invalid/repo'}, {**https, 'password': 'bad\x00value'},
                     {**ssh, 'known_hosts': '* ssh-ed25519 AAAA\n'},
                     {**ssh, 'known_hosts': '@cert-authority *.invalid ssh-ed25519 AAAA\n'},
                     {**ssh, 'origin': 'git@git.example.invalid:repo'},
                     {**https, 'origin': 'https://user:password@git.example.invalid/repo'},
                     {**https, 'origin': 'https://git.example.invalid/repo?query'},
                     {**https, 'origin': 'https://git.example.invalid/../repo'}):
            with self.subTest(format=item['format']), self.assertRaises(Refused):
                m.SourceCredential.parse(item, https['origin'] if item.get('format') != 'git-ssh-key-v1' else ssh['origin'])

    def test_ssh_helper_accepts_only_exact_git_origin_operands_and_compiles_pinned_transport(self):
        from types import SimpleNamespace
        m = api('host_phase_source')
        self.assertTrue(hasattr(m, 'ssh_command'), 'SSH helper only searches authority membership instead of exact operands')
        source = SimpleNamespace(origin='ssh://git@source.invalid:2222/team/repo.git', format='git-ssh-key-v1')
        argv = ('-o', 'SendEnv=GIT_PROTOCOL', '-p', '2222', 'git@source.invalid', "git-upload-pack '/team/repo.git'")
        compiled = m.ssh_command(source, argv, Path('/usr/bin/ssh'), Path('/srv/body'))
        self.assertIn('StrictHostKeyChecking=yes', compiled)
        self.assertIn('IdentityAgent=none', compiled)
        self.assertIn('ProxyCommand=none', compiled)
        self.assertEqual(compiled[-3:], ('--', 'git@source.invalid', argv[-1]))
        for poisoned in ((*argv, '--extra'), ('-o', 'ProxyCommand=evil', *argv),
                         (*argv[:-1], "git-upload-pack '/other.git'"),
                         (*argv[:3], '22', *argv[4:]), (*argv[:4], 'foreign@source.invalid', argv[-1])):
            with self.assertRaises(Refused): m.ssh_command(source, poisoned, Path('/usr/bin/ssh'), Path('/srv/body'))

    def test_compiled_git_environment_and_operands_cannot_inherit_node_or_git_poison(self):
        m = api('host_phase_source')
        env = m.git_environment(Path('/srv/mop/b/fixture'), Path('/opt/runtime/empty'), Path('/opt/runtime/ca.pem'))
        self.assertEqual(env['GIT_CONFIG_NOSYSTEM'], '1')
        self.assertEqual(env['GIT_CONFIG_GLOBAL'], '/dev/null')
        self.assertEqual(env['GIT_CONFIG_SYSTEM'], '/dev/null')
        self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')
        self.assertNotIn('SSH_AUTH_SOCK', env)
        self.assertNotIn('HTTPS_PROXY', env)
        for variable in ('GIT_CONFIG_PARAMETERS', 'GIT_SSH_COMMAND', 'GIT_AUTHOR_NAME', 'NODE_BEARER', 'PYTHONPATH'):
            self.assertNotIn(variable, env)
        argv = m.identity_commands(Path('/usr/bin/git'), '--hostile-name', 'owner@example.invalid')
        self.assertEqual(len(argv), 3)
        for item in argv:
            self.assertIn('--', item)
            self.assertLess(item.index('--'), len(item)-2)
        self.assertEqual(argv[0][-2:], ('user.name', '--hostile-name'))


class ResourceTests(unittest.TestCase):
    def test_cpu_mhz_uses_explicit_installed_capacity_and_disk_is_reservation_not_quota(self):
        m = api('host_phase_manifest')
        profile = m.Capacity(cpu_mhz_per_core=2000, cpu_weight=75, tasks_max=64,
                             memory_mb_max=4096, cpu_mhz_max=8000, disk_mode='shared-reservation')
        limits = profile.project(dict(policy_id=str(uuid4()), cpu=500, memory_mb=128, disk_mb=64))
        self.assertEqual(limits.memory_max, 128*1024*1024)
        self.assertEqual(limits.cpu_quota_usec, 250000)
        self.assertEqual(limits.properties()['CPUQuota'], '25%')
        rounded = replace(profile, cpu_mhz_per_core=3000).project(dict(policy_id=str(uuid4()), cpu=500, memory_mb=128, disk_mb=64))
        self.assertEqual(rounded.cpu_quota_usec, 166600)
        self.assertEqual(rounded.properties()['CPUQuota'], '16.66%')
        self.assertEqual(limits.cpu_weight, 75)
        self.assertEqual(limits.tasks_max, 64)
        self.assertEqual(limits.disk_mode, 'shared-reservation')
        self.assertFalse(limits.disk_isolation)
        self.assertEqual(limits.disk_reservation_bytes, 64*1024*1024)
        with self.assertRaises(Refused):
            replace(profile, disk_mode='hard-quota')
        for policy in (dict(cpu=0, memory_mb=128, disk_mb=64), dict(cpu=True, memory_mb=128, disk_mb=64),
                       dict(cpu=9000, memory_mb=128, disk_mb=64), dict(cpu=500, memory_mb=8192, disk_mb=64)):
            with self.assertRaises(Refused): profile.project(dict(policy_id=str(uuid4()), **policy))

    def test_systemd_duration_readback_preserves_exact_microseconds_without_float_guesses(self):
        m = api('host_phase_manifest')
        self.assertTrue(hasattr(m, 'duration_usec'), 'resource readback cannot parse actual systemd time units')
        for text, value in (('250ms', 250000), ('100ms', 100000), ('1s', 1000000), ('250000us', 250000), ('1.250s', 1250000)):
            self.assertEqual(m.duration_usec(text), value)
        for text in ('infinity', '0', '-1s', '1MHz', 'nan', '1.0000001s'):
            with self.assertRaises(Refused): m.duration_usec(text)

    def test_resource_receipt_accepts_normalized_systemd_readback_but_never_different_units(self):
        m = api('host_phase_manifest')
        limits = m.Capacity(2000, 75, 64, 4096, 8000, 'shared-reservation').project(dict(
            policy_id=str(uuid4()), cpu=500, memory_mb=128, disk_mb=64))
        values = dict(MemoryMax=str(128*1048576), TasksMax='64', CPUWeight='75',
                      CPUQuotaPerSecUSec='250ms', CPUQuotaPeriodUSec='100ms')
        limits.verify(values)
        for key in values:
            with self.subTest(key=key), self.assertRaises(Refused):
                limits.verify({**values, key: '1'})

    def test_manifest_binds_every_installed_root_body_source_and_actual_fixed_runner(self):
        from mop.node.host_root import ROOT_SOURCES, PHASE_ROOT_SOURCES
        from mop.node.host_body import BODY_SOURCES
        m = api('host_phase_manifest')
        release = Path('/opt/phase-release')
        required = set(ROOT_SOURCES) | set(PHASE_ROOT_SOURCES) | set(BODY_SOURCES)
        tools = {name: release/name for name in m.TOOLS}
        tools['runner'] = release/'mop/node/host_body.py'
        artifacts = tuple(dict(path=str(release/name), sha256='a'*64, executable=False) for name in required)
        manifest = m.RuntimeManifest(m.Capacity(2000, 75, 64, 4096, 8000, 'shared-reservation'), (), tools, {}, artifacts, 'b'*64)
        manifest.require_release(release)
        with self.assertRaises(Refused):
            replace(manifest, artifacts=artifacts[1:]).require_release(release)
        with self.assertRaises(Refused):
            replace(manifest, tools={**tools, 'runner': release/'foreign-runner'}).require_release(release)

    def test_manifest_cannot_describe_fixed_executable_as_nonexecutable_data(self):
        from mop.node.host_inputs import canonical_json
        m = api('host_phase_manifest')
        tools = {name: '/opt/runtime/'+name for name in m.TOOLS}
        image, policy = str(uuid4()), str(uuid4())
        value = dict(schema_version=1, capacity=dict(cpu_mhz_per_core=2000, cpu_weight=75, tasks_max=64,
            memory_mb_max=4096, cpu_mhz_max=8000, disk_mode='shared-reservation'),
            images=[dict(image_revision=image, logical_pin=hashlib.sha256(('image-revision:'+image).encode()).hexdigest(), policy_id=policy)],
            tools=tools, directories={name: '/opt/runtime/'+name for name in m.DIRECTORIES},
            artifacts=[dict(path=filename, sha256='a'*64, executable=name not in ('ca', 'ansible_config')) for name, filename in tools.items()])
        m.RuntimeManifest.parse(canonical_json(value))
        for item in value['artifacts']:
            if not item['executable']: continue
            changed = copy.deepcopy(value)
            next(a for a in changed['artifacts'] if a['path'] == item['path'])['executable'] = False
            with self.subTest(path=item['path']), self.assertRaises(Refused):
                m.RuntimeManifest.parse(canonical_json(changed))

    def test_manifest_requires_real_hashes_and_fixed_root_owned_runtime_not_logical_image_pin(self):
        m = api('host_phase_manifest')
        self.assertTrue(callable(m.RuntimeManifest.load))
        self.assertIn('root_owned', inspect.signature(m.verify_artifact).parameters)
        for raw in (b'{}', b'[]', b'{"schema_version":1,"image_revision":"'+str(uuid4()).encode()+b'"}'):
            with self.assertRaises(Refused): m.RuntimeManifest.parse(raw)


class CleanupCompileTests(unittest.TestCase):
    def test_only_the_pinned_removed_cgroup_inode_can_prove_manager_gc_after_kill(self):
        from types import SimpleNamespace
        from mop.node import host_scope_systemd as m
        import stat
        self.assertTrue(hasattr(m, 'removed_cgroup_empty'), 'live MainPID retirement loses empty proof when systemd removes its cgroup')
        before = SimpleNamespace(st_dev=7, st_ino=19, st_mode=stat.S_IFDIR|0o755, st_nlink=2)
        # HYPOTHESIS nlink=0 отменена: actual kernfs оставляет nlink=2.
        # SOLUTION: same pinned inode + exact kernel fd d_path deleted marker.
        _, scope, _ = selected()
        removed = SimpleNamespace(st_dev=7, st_ino=19, st_mode=stat.S_IFDIR|0o755, st_nlink=2)
        pathname = '/sys/fs/cgroup'+scope.cgroup
        self.assertTrue(m.removed_cgroup_empty(before, removed, pathname+' (deleted)', scope))
        self.assertFalse(m.removed_cgroup_empty(before, removed, pathname, scope))
        self.assertFalse(m.removed_cgroup_empty(before, removed, pathname+'-foreign (deleted)', scope))
        self.assertFalse(m.removed_cgroup_empty(before, removed, '/tmp/foreign (deleted)', scope))
        self.assertFalse(m.removed_cgroup_empty(before, SimpleNamespace(st_dev=7, st_ino=20, st_mode=stat.S_IFDIR|0o755, st_nlink=2), pathname+' (deleted)', scope))

    def test_cleanup_runner_has_its_own_bounded_body_uid_cgroup_and_no_project_command(self):
        from mop.node import host_scope_systemd as m
        from mop.node.host_scope import Installed
        self.assertTrue(hasattr(m, 'cleanup_argv'), 'cleanup CLI timeout can orphan an unbounded body cleanup unit')
        _, scope, _ = selected()
        installed = Installed(21071, 21072, 21072, Path('/opt/mop/mop/node/host_body.py'),
            Path('/srv/mop/b'), Path('/var/lib/mop-node'))
        limits = api('host_phase_manifest').Capacity(2000, 75, 64, 4096, 8000, 'shared-reservation').project(dict(
            policy_id=str(uuid4()), cpu=500, memory_mb=128, disk_mb=64))
        argv = m.cleanup_argv(installed, scope, limits)
        self.assertIn('--property=User=21072', argv)
        self.assertIn('--property=RuntimeMaxSec=8s', argv)
        self.assertIn('--property=TimeoutStopSec=2s', argv)
        self.assertIn('--property=RemainAfterExit=yes', argv)
        self.assertEqual(argv[-4:], ('--', str(installed.runner), '--cleanup', scope.unit))


class CgroupOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_retirement_retains_exact_pinned_cgroup_before_broker_exit_and_manager_gc(self):
        import os
        import tempfile
        from unittest.mock import patch
        from mop.node import host_scope_systemd as m
        from mop.node.host_scope import Facts, Installed, Unknown
        self.assertTrue(hasattr(m.SystemdCgroupAdapter, 'retain_phase_cgroup'),
            'broker failure loses kernel cleanup proof before retirement starts')
        _, scope, _ = selected()
        installed = Installed(21071, 21072, 21072, Path('/opt/mop/mop/node/host_body.py'),
            Path('/srv/mop/b'), Path('/var/lib/mop-node'))
        facts = Facts(scope, uuid4().hex, str(uuid4()), installed.body_uid, installed.body_gid,
            str(installed.runner), (str(installed.runner), '--scope', scope.unit), scope.cgroup, True, True)
        reference = facts.verify(installed, scope).reference
        adapter = m.SystemdCgroupAdapter.__new__(m.SystemdCgroupAdapter)
        adapter.installed = installed
        adapter._phase_cgroups = {}
        calls = []
        async def inspect(scope):
            calls.append(scope)
            return None
        adapter.inspect = inspect
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent)/'pinned'
            directory.mkdir()
            adapter._cgroup = lambda scope: os.open(directory, os.O_RDONLY|os.O_DIRECTORY)
            adapter.retain_phase_cgroup(facts)
            directory.rmdir()
            with patch.object(m.os, 'readlink', return_value='/sys/fs/cgroup'+scope.cgroup+' (deleted)'):
                empty = await adapter.retire(reference)
            self.assertFalse(empty.populated)
            self.assertEqual(calls, [])
            async def disappeared(scope):
                raise Refused('HOST actual ControlGroup mismatch or unavailable')
            adapter.inspect = disappeared
            with patch.object(m.os, 'readlink', side_effect=[
                    '/sys/fs/cgroup'+scope.cgroup, '/sys/fs/cgroup'+scope.cgroup+' (deleted)']):
                empty = await adapter.retire(reference)
            self.assertFalse(empty.populated, 'manager GC raced the first retained-fd observation')
            adapter.inspect = inspect
            with patch.object(m.os, 'readlink', side_effect=[
                    '/sys/fs/cgroup'+scope.cgroup, '/sys/fs/cgroup'+scope.cgroup+' (deleted)']):
                empty = await adapter.retire(reference)
            self.assertFalse(empty.populated, 'not-found manager GC raced the first retained-fd observation')
            with self.assertRaises(Unknown):
                await adapter.retire(replace(reference, invocation_id=uuid4().hex))
            for entry in adapter._phase_cgroups.values(): os.close(entry.fd)


class RunnerImageTests(unittest.TestCase):
    def test_same_pid_start_and_uid_cannot_replace_the_fixed_broker_image_or_argv(self):
        m = api()
        self.assertTrue(hasattr(m, 'verify_runner_image'), 'body peer can retain PID/start but exec another image')
        _, scope, _ = selected()
        python, runner = '/usr/local/bin/python3.12', '/opt/mop/mop/node/host_body.py'
        argv = ('/usr/bin/python3', '-ISB', runner, '--scope', scope.unit)
        m.verify_runner_image(python, argv, python, runner, scope)
        for exe, args in (('/usr/bin/sh', argv), (python, ('/usr/bin/python3', '-c', 'pass')),
                          (python, (*argv[:-1], 'foreign.service')), (python, (*argv, '--unsafe'))):
            with self.assertRaises(Refused): m.verify_runner_image(exe, args, python, runner, scope)


class BodyInputTests(unittest.TestCase):
    def test_cleanup_only_entry_does_not_spend_original_reserve_rehashing_git_ansible_tools(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from mop.node import host_body as m
        from mop.node.host_phase_supervisor import PhaseConfig
        from mop.node.host_phase_manifest import RuntimeManifest
        config = SimpleNamespace(installed=SimpleNamespace(body_uid=21072, body_gid=21072, runner=Path(m.__file__).absolute()))
        with patch.object(PhaseConfig, 'load', return_value=config), patch.object(m.os, 'geteuid', return_value=21072), \
                patch.object(m.os, 'getegid', return_value=21072), patch.object(RuntimeManifest, 'load', side_effect=AssertionError('cleanup must not load project tools')):
            actual, manifest = m.load_installation(Path('/opt/release'), cleanup=True)
        self.assertIs(actual, config)
        self.assertIsNone(manifest)

    def test_private_helper_read_does_not_mistake_access_time_for_content_mutation(self):
        import os
        import tempfile
        from mop.node.host_body import secret_read
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory)/'source.json'
            filename.write_bytes(b'{"format":"fixture-only"}')
            filename.chmod(0o600)
            os.utime(filename, ns=(1, 2))
            self.assertEqual(secret_read(filename), filename.read_bytes())


class CheckpointTests(unittest.TestCase):
    def test_approval_is_connection_bound_single_use_and_next_effect_is_not_preapproved(self):
        m = api()
        self.assertTrue(hasattr(m, 'Checkpoint'), 'root has no single-use finite checkpoint state')
        c = m.Checkpoint('git-fetch', 5)
        connection, foreign = object(), object()
        challenge = c.challenge(connection)
        with self.assertRaises(Refused): c.approve(foreign, challenge)
        with self.assertRaises(Refused): c.approve(connection, str(uuid4()))
        self.assertFalse(c.approved)
        c.approve(connection, challenge)
        self.assertTrue(c.approved)
        with self.assertRaises(Refused): c.approve(connection, challenge)
        c2 = m.Checkpoint('git-checkout', 6)
        with self.assertRaises(Refused): c2.approve(connection, challenge)
        self.assertFalse(c2.approved)

    def test_reconnect_invalidates_outstanding_response_but_keeps_pending_effect_owner(self):
        m = api()
        self.assertTrue(hasattr(m, 'Checkpoint'), 'root has no checkpoint disconnect invalidation')
        c = m.Checkpoint('ansible', 1)
        first, second = object(), object()
        previous = c.challenge(first)
        c.disconnect(first)
        current = c.challenge(second)
        self.assertNotEqual(previous, current)
        with self.assertRaises(Refused): c.approve(first, previous)
        with self.assertRaises(Refused): c.approve(second, previous)
        c.approve(second, current)
        self.assertTrue(c.approved)


if __name__ == '__main__':
    unittest.main()
