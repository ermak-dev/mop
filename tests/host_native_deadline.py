#!/usr/bin/env python3
"""HYPOTHESIS: expected MCP argv строится из inspected argv, не Root window.
SPEC: один original Root deadline min(intent, original start+300), immutable
scope/attempt/Reference binding. Синтетические proc ports, не vendor acceptance.
SOLUTION: Root capture до submission, retained frozen Admission/LaunchWindow,
finite native-input checkpoint отдаёт exact значение, Root argv сравнивает с ним.
RESULT: expired/extended/regressed/reconnect/missing-window и checkpoint wait
RED -> GREEN; exact lifetime observation не renew и не новый startup lease.
Body native-launch на истёкшем original — Refused с причиной, не ValueError.
STATUS: FIXED — see #784
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
import shlex
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import _lib  # noqa: F401
import hermetic  # noqa: F401
from _host_native import NativeProofFixture
from mop.common.management_context import body_deadline
from mop.node.host_handoff import LaunchWindow
from mop.node.host_scope import Refused
from mop.node.host_backend import Process


class OriginalDeadline(NativeProofFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.original_start = datetime.now(timezone.utc)-timedelta(seconds=1)
        self.window = LaunchWindow(self.original_start, self.admission.work.not_after)
        # Явный Root retained-state port; старый validator его игнорирует.
        self.adapter._native_launches = {self.reference: (self.admission, self.window)}
        self.deadline = self.window.deadline.isoformat()
        await self.listener()

    async def proof(self):
        return await self.adapter.native_launch(self.reference,
            dict(pid=self.native.pid, process_start=self.native.boot_id+':'+str(self.native.start),
                session_id=self.session, tmux_pid=self.tmux.pid, tmux_start=self.tmux.start),
            self.tools, self.pins)

    async def test_expired_argv_is_not_the_root_original_deadline(self):
        self.deadline = '2000-01-01T00:00:00+00:00'
        with self.assertRaises(Refused):
            await self.proof()

    async def test_far_future_argv_is_not_the_root_original_deadline(self):
        self.deadline = '2099-01-01T00:00:00+00:00'
        with self.assertRaises(Refused):
            await self.proof()

    async def test_general_300_bound_is_not_original_intent_authority(self):
        self.deadline = (datetime.now(timezone.utc)+timedelta(seconds=250)).isoformat()
        self.assertLess(self.window.deadline, body_deadline(self.deadline))
        with self.assertRaises(Refused):
            await self.proof()

    async def test_shorter_or_extended_deadline_is_not_exact_original(self):
        for seconds in (-1, 1):
            self.deadline = (self.window.deadline+timedelta(seconds=seconds)).isoformat()
            with self.subTest(seconds=seconds), self.assertRaises(Refused):
                await self.proof()

    async def test_normal_exact_original_deadline_and_repeat_are_accepted(self):
        first = await self.proof()
        self.assertEqual(await self.proof(), first)
        self.assertEqual(await self.attach(), await self.attach())

    async def test_reconnect_observation_does_not_refresh_original_window(self):
        first = await self.proof()
        self.deadline = (self.window.deadline+timedelta(seconds=10)).isoformat()
        with self.assertRaises(Refused):
            await self.proof()
        self.deadline = self.window.deadline.isoformat()
        self.assertEqual(await self.proof(), first)
        self.assertIs(self.adapter._native_launches[self.reference][1], self.window)

    async def test_missing_original_root_window_cannot_be_reconstructed_from_argv(self):
        self.adapter._native_launches.clear()
        with self.assertRaises(Refused):
            await self.proof()

    async def test_root_capture_is_once_per_reference_and_binds_original_attempt(self):
        retain = getattr(self.adapter, 'retain_native_window', None)
        self.assertTrue(callable(retain), 'Root original native window retention is missing')
        current = getattr(self.adapter, 'native_window', None)
        self.assertTrue(callable(current), 'Root original window binding lookup is missing')
        self.adapter._native_launches.clear()
        window = retain(self.reference, self.admission)
        self.assertEqual(window.intent_expiry, self.admission.work.not_after)
        self.assertEqual(window.deadline, min(self.admission.work.not_after,
            window.launched_at+timedelta(seconds=300)))
        self.assertIs(current(self.reference, self.admission), window)
        with self.assertRaises(Refused):
            retain(self.reference, self.admission)
        for reference, admission in (
                (replace(self.reference, invocation_id=uuid4().hex), self.admission),
                (replace(self.reference, boot_id=str(uuid4())), self.admission),
                (replace(self.reference, scope=replace(self.scope, target_id=str(uuid4()))), self.admission),
                (self.reference, replace(self.admission, attempt_id=str(uuid4()))),
                (self.reference, replace(self.admission, request_id=str(uuid4()))),
                (self.reference, replace(self.admission, work=replace(self.admission.work,
                    not_after=self.admission.work.not_after+timedelta(seconds=1))))):
            with self.subTest(reference=reference, attempt=admission.attempt_id), self.assertRaises(Refused):
                current(reference, admission)
        self.assertIs(current(self.reference, self.admission), window)

    async def test_root_capture_preserves_300_cap_without_phase_lease_as_new_window(self):
        retain = getattr(self.adapter, 'retain_native_window', None)
        self.assertTrue(callable(retain), 'Root original native window retention is missing')
        self.adapter._native_launches.clear()
        longer = replace(self.admission, work=replace(self.admission.work,
            not_after=datetime.now(timezone.utc)+timedelta(seconds=900)))
        window = retain(self.reference, longer)
        self.assertEqual(window.deadline, window.launched_at+timedelta(seconds=300))
        # Phase lease остаётся прежним и не становится renewable MCP deadline.
        self.assertGreater(window.deadline, longer.lease_until)

    async def test_capture_refuses_expired_admission_and_foreign_scope(self):
        self.adapter._native_launches.clear()
        for reference, admission in (
                (replace(self.reference, scope=replace(self.scope, launch_id=str(uuid4()))), self.admission),
                (self.reference, replace(self.admission,
                    lease_until=datetime.now(timezone.utc)-timedelta(seconds=1)))):
            with self.subTest(reference=reference), self.assertRaises(Refused):
                self.adapter.retain_native_window(reference, admission)
        self.assertEqual(self.adapter._native_launches, {})

    async def test_expired_original_is_not_recaptured_but_physical_argv_remains_observable(self):
        # После startup cutoff exact argv остаётся physical fact, не новый lease.
        # Handoff/effect deadline истекает, но observation не убивает producer.
        with patch('mop.node.host_handoff._now', return_value=self.window.deadline+timedelta(seconds=1)):
            with self.assertRaises(Refused):
                self.window.check()
            await self.proof()

    async def test_submission_captures_original_window_before_scheduling_body_effects(self):
        import sys
        from pathlib import Path
        from mop.node.host_inputs import pack
        from mop.node.host_native import ModelInputs, NativeSubmission, ProviderProfile
        from mop.node.host_native_owner import submit_native
        from mop.node.host_phases import Projection
        profile = ProviderProfile('owned-fixture-v1', 'https://provider.invalid', Path('/opt/fixed/ca'), 'api-key-v1')
        envelope = pack(self.admission, self.scope, self.delivery)
        projection = Projection.from_delivery(self.delivery)
        request = NativeSubmission(self.admission, envelope.descriptor, projection, self.reference,
            ModelInputs.parse(self.delivery, profile).pins())
        body = SimpleNamespace(reference=self.reference, native=None, projection=projection,
            completed=['prepare_body', 'bootstrap_body', 'initial_identity'],
            attempts=[SimpleNamespace(acknowledged=True)], cleanup=None)
        store = Mock()
        root = SimpleNamespace(_closed=False, _confirmed=True, _bound_epoch=self.scope.epoch,
            identity=SimpleNamespace(node_id=self.scope.node_id), _backend_epoch=self.scope.executor_epoch,
            attempts={}, capacity=20, bodies={self.scope: body}, _owned={self.scope: self.reference},
            # #781: pre-dispatch status budget reads the installed manifest;
            # fake mirrors the real RuntimeManifest surface, not the assertions.
            manifest=SimpleNamespace(
                project=Mock(return_value=SimpleNamespace(wire=lambda: dict(
                    image_revision='image', policy_id='policy'))),
                require_native_release=Mock(), verify=Mock(),
                content_digest='a'*64, directories=dict(python_root=sys.path[0]),
                tools=dict(ca=profile.ca, native=Path('/opt/tools/native'), tmux=Path('/opt/tools/tmux'))),
            installed=SimpleNamespace(runner=self.adapter.installed.runner, node_uid=os.getuid()),
            _store=Mock(return_value=store), config=SimpleNamespace(snapshots=self.home), adapter=self.adapter)
        self.adapter._native_launches.clear()
        scheduled = []
        async def run(owner, retained_body, item):
            scheduled.append(self.adapter.native_window(request.reference, item.submission.admission))
        with patch('mop.node.host_native_body.installed_profile', return_value=profile), \
                patch('mop.node.host_native_owner._read', return_value=envelope.raw), \
                patch('mop.node.host_native_owner.run_native', run):
            item = submit_native(root, request)
            # create_task ещё не исполнялся: Root state уже существует.
            self.assertEqual(scheduled, [])
            original = self.adapter.native_window(self.reference, self.admission)
            self.assertEqual(original.intent_expiry, self.admission.work.not_after)
            self.assertIs(body.native.item, item)
            await item.task
            self.assertEqual(scheduled, [original])
            with self.assertRaises(Refused):
                submit_native(root, request)
        self.assertIs(self.adapter.native_window(self.reference, self.admission), original)

    async def test_root_checkpoint_delivers_only_the_retained_original_deadline(self):
        from mop.node.host_native_owner import NativeOwner
        root = SimpleNamespace(_peer=AsyncMock(), checkpoint=AsyncMock(), adapter=self.adapter)
        body = SimpleNamespace(reference=self.reference)
        item = SimpleNamespace(submission=SimpleNamespace(admission=self.admission))
        owner = NativeOwner(root, body, item, None)
        owner.reply = AsyncMock()
        identifier = str(uuid4())
        await owner.request(dict(id=identifier, operation='checkpoint',
            payload=dict(effect='native-input', sequence=0)))
        owner.reply.assert_awaited_once_with(identifier, answer=dict(approved='native-input', sequence=0,
            launcher_deadline=self.window.deadline.isoformat()))

    async def test_expired_window_cannot_approve_new_launch_after_checkpoint_wait(self):
        from mop.node.host_native_owner import NativeOwner
        async def delayed_approval(*args):
            self.clock.return_value = self.window.deadline+timedelta(seconds=1)
        root = SimpleNamespace(_peer=AsyncMock(), checkpoint=AsyncMock(side_effect=delayed_approval),
            adapter=self.adapter)
        owner = NativeOwner(root, SimpleNamespace(reference=self.reference),
            SimpleNamespace(submission=SimpleNamespace(admission=self.admission)), None)
        owner.reply = AsyncMock()
        with patch('mop.node.host_handoff._now', return_value=self.original_start) as self.clock:
            await owner.request(dict(id='owned-request', operation='checkpoint',
                payload=dict(effect='native-launch', sequence=1)))
        owner.reply.assert_awaited_once_with('owned-request',
            error='original native management handoff deadline expired')
        self.assertIs(self.adapter._native_launches[self.reference][1], self.window)

    async def test_body_rejects_missing_expired_extended_or_replaced_root_deadline(self):
        from mop.node.host_native_body import NativeBody
        approved = dict(approved='native-input', sequence=0)
        good = (datetime.now(timezone.utc)+timedelta(seconds=20)).isoformat()
        for result in (approved,
                approved | dict(launcher_deadline='2000-01-01T00:00:00+00:00'),
                approved | dict(launcher_deadline=(self.admission.work.not_after+timedelta(seconds=1)).isoformat()),
                approved | dict(launcher_deadline=good, arbitrary='not-allowed')):
            owner = NativeBody.__new__(NativeBody)
            owner.launcher_deadline = None
            owner.submission = SimpleNamespace(admission=self.admission)
            owner.channel = SimpleNamespace(request=AsyncMock(return_value=result))
            owner.broker = SimpleNamespace()
            with self.subTest(result=result), self.assertRaises(Refused):
                await owner.checkpoint('native-input', 0)
            self.assertIsNone(owner.launcher_deadline)
            self.assertFalse(hasattr(owner.broker, 'effect'))
        owner.channel.request.return_value = approved | dict(launcher_deadline=good)
        await owner.checkpoint('native-input', 0)
        original = owner.launcher_deadline
        owner.channel.request.return_value = approved | dict(
            launcher_deadline=(original+timedelta(seconds=1)).isoformat())
        with self.assertRaises(Refused):
            await owner.checkpoint('native-input', 0)
        self.assertIs(owner.launcher_deadline, original)

    async def test_body_launch_checkpoint_refuses_expired_original_with_reason(self):
        # Root approval не продлевает original: истёкший retained deadline —
        # Refused с причиной, не голый ValueError, теряющий её в body_failed.
        from mop.node.host_native_body import NativeBody
        owner = NativeBody.__new__(NativeBody)
        owner.submission = SimpleNamespace(admission=self.admission)
        owner.broker = SimpleNamespace(effect='native-input')
        owner.channel = SimpleNamespace(request=AsyncMock(
            return_value=dict(approved='native-launch', sequence=1)))
        for deadline in (None, datetime.now(timezone.utc)-timedelta(seconds=1)):
            owner.launcher_deadline = deadline
            with self.subTest(deadline=deadline), self.assertRaisesRegex(Refused, 'original root launcher deadline'):
                await owner.checkpoint('native-launch', 1)
            self.assertEqual(owner.broker.effect, 'native-input')

    async def test_body_launch_uses_root_value_not_a_fresh_local_window(self):
        from mop.driver.launch import RegisteredLayout, Registration
        from mop.node.host_native_body import NativeBody
        # Body now намного позже original start; approved Root deadline короче.
        deadline = (datetime.now(timezone.utc)+timedelta(seconds=20)).isoformat()
        registration = Registration(self.admission.work, str(uuid4()), 'b'*64, 'c'*64, 'pu-owned-1')
        # Listener занимает r; native body launch требует ещё absent runtime.
        separate = self.home/'owned-body'
        separate.mkdir(mode=0o700)
        layout = RegisteredLayout(separate, registration, profile='host-native-v1')
        tools = self.tools | dict(git=self.tools['native'], hook=self.tools['python'])
        broker = SimpleNamespace(manifest=SimpleNamespace(tools=tools, require_native_release=Mock()),
            config=SimpleNamespace(installed=self.adapter.installed), scope=self.scope,
            write=lambda path, data: path.write_bytes(data))
        owner = NativeBody.__new__(NativeBody)
        owner.started, owner.broker, owner.layout = False, broker, layout
        owner.launcher_deadline = None
        owner.submission = SimpleNamespace(admission=self.admission)
        owner.inputs = SimpleNamespace(bootstrap_model='fixture-bootstrap', environment=Mock(return_value={}))
        owner.profile = None
        owner.runtime = SimpleNamespace(prepare=Mock(return_value={}))
        async def reply(operation, payload):
            if operation == 'native_launched':
                return dict(retained=True)
            value = dict(approved=payload['effect'], sequence=payload['sequence'])
            return value | dict(launcher_deadline=deadline) if payload['effect'] == 'native-input' else value
        owner.channel = SimpleNamespace(request=AsyncMock(side_effect=reply))
        owner.tmux = AsyncMock(return_value=f'{os.getpid()}|{os.getpid()}'.encode())
        with patch('os.readlink', return_value=str(tools['native'])), patch.object(Process, 'read',
                return_value=Process(os.getpid(), 900, self.reference.boot_id)):
            await owner.launch()
        command = shlex.split(owner.tmux.await_args_list[0].args[-1])[1:]
        config = json.loads(command[-1])['mcpServers']['mop']
        self.assertEqual(config['args'][-2:], ['--deadline', deadline])


if __name__ == '__main__':
    unittest.main()
