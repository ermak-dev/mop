"""Настоящие Unix peers, не native CLI и не application acceptance прогона.

HYPOTHESIS: #729 bridge принимает придуманный ACK, теряет все native statuses
и выдаёт любую idle state за idle. Native pn(delivered) означает approved and
released previously-held input, а не normal enqueue и не успешный model turn
(исследованный 2.1.296, chunk-1428.js, docs/CLAUDECODE.md §3.9).
SPEC: только delivered подтверждает release; held ждёт terminal receipt;
отказ конечен, dropped IDs ограничены и относятся лишь к pending user IDs.
Idle — отдельная одноразовая подписка; exited/unavailable не означают idle.
SOLUTION: только peer_message_status; delivered доказывает release, terminal
отказ даёт unavailable; отдельный Future[bool] хранит одноразовый idle outcome.
RESULT: Unix peers покрывают все шесть statuses, malformed/late/duplicate,
лимиты и shutdown; native normal Docker 2.1.302 подтвердил msgV=1 idle и
коррелированный dropped. Native held/delivered не наблюдались: release остаётся
unknown, обычный enqueue не получает придуманного ACK. Не полный native green.
STATUS: FIXED — see #729 (bridge conformance, не production enablement).
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

import hermetic  # noqa: F401
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mop.client.local_bridge import LocalBridge, OwnSession
from mop.common.endpoint import DeliveryFrame


class NativeReceipts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='m729-', dir='/tmp')
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'external.sock')
        self.frames = asyncio.Queue()
        self.connections = set()
        async def external(reader, writer):
            task = asyncio.current_task()
            self.connections.add(task)
            try:
                line = await reader.readline()
                if line:
                    value = json.loads(line)
                    if value['type'] == 'auth':
                        value = json.loads(await reader.readline())
                    await self.frames.put(value)
            finally:
                writer.close()
                await writer.wait_closed()
                self.connections.discard(task)
        self.server = await asyncio.start_unix_server(external, path=self.path)
        os.chmod(self.path, 0o600)
        self.bridge = LocalBridge(OwnSession(self.path, os.getpid(), 'own-conversation'), timeout=.08)
        await self.bridge.start()

    async def asyncTearDown(self):
        callback = self.bridge._callback
        await self.bridge.aclose()
        self.server.close()
        await self.server.wait_closed()
        if self.connections:
            await asyncio.gather(*self.connections)
        self.assertTrue(callback is None or not callback.exists())
        self.assertFalse(self.bridge._pending)
        self.assertFalse(self.bridge._tasks)
        self.assertFalse(self.bridge._writers)
        self.assertFalse(self.bridge._outbound)
        self.assertFalse(self.bridge._idle)

    def frame(self, idle=False):
        return DeliveryFrame(uuid4(), uuid4(), 'own-conversation', 'private fixture '+uuid4().hex, 'next', idle)

    async def begin(self, idle=False):
        frame = self.frame(idle)
        task = asyncio.create_task(self.bridge.deliver(frame))
        wire = await asyncio.wait_for(self.frames.get(), 1)
        self.assertEqual(wire['msg_id'], str(frame.message_id))
        return frame, task

    async def callback(self, *, raw=None, **fields):
        if raw is None:
            raw = (json.dumps(dict(msgV=1, msg_id=str(uuid4()), type='control', **fields))+'\n').encode()
        reader, writer = await asyncio.open_unix_connection(str(self.bridge._callback))
        writer.write(raw)
        await writer.drain()
        # EOF доказывает, что обработчик callback закончил разбор, не sleep.
        writer.write_eof()
        await asyncio.wait_for(reader.read(), 1)
        writer.close()
        await writer.wait_closed()

    async def status(self, message_id, status, **fields):
        await self.callback(action='peer_message_status', orig_msg_id=str(message_id), status=status, **fields)

    async def subscribe(self):
        frame, task = await self.begin(True)
        await self.status(frame.message_id, 'delivered')
        result = await task
        self.assertEqual(result.outcome, 'accepted')
        control = await asyncio.wait_for(self.frames.get(), 1)
        self.assertEqual(control['action'], 'notify_when_idle')
        self.assertNotEqual(control['msg_id'], str(frame.message_id))
        return frame, control['msg_id']

    async def test_all_native_statuses_have_only_sufficient_acceptance_evidence(self):
        for status, expected in (('held', 'unknown'), ('denied', 'unavailable'),
                ('expired', 'unavailable'), ('delivered', 'accepted'),
                ('refused', 'unavailable'), ('dropped', 'unavailable'),
                ('accepted', 'unknown'), ('unknown-future-status', 'unknown'), ('', 'unknown')):
            with self.subTest(status=status):
                frame, task = await self.begin()
                await self.status(frame.message_id, status)
                self.assertEqual((await task).outcome, expected)
                self.assertEqual((await self.bridge.deliver(frame)).outcome, expected)
                self.assertTrue(self.frames.empty(), 'no normal enqueue ACK or automatic replay')
        frame, task = await self.begin()
        await self.status(frame.message_id, 'expired', status_detail='refused')
        self.assertEqual((await task).outcome, 'unavailable')

    async def test_held_is_nonterminal_until_correlated_release_or_refusal(self):
        for terminal, expected in (('delivered', 'accepted'), ('denied', 'unavailable')):
            with self.subTest(terminal=terminal):
                frame, task = await self.begin()
                await self.status(frame.message_id, 'held')
                self.assertFalse(task.done())
                await self.status(uuid4(), terminal)
                self.assertFalse(task.done())
                await self.status(frame.message_id, terminal)
                self.assertEqual((await task).outcome, expected)

    async def test_invented_actions_never_prove_acceptance_or_refusal(self):
        for action in ('message_accepted', 'message_refused', 'accepted', 'rename', 'control_request'):
            with self.subTest(action=action):
                frame, task = await self.begin()
                await self.callback(action=action, orig_msg_id=str(frame.message_id), status='delivered', state='idle')
                result = await task
                self.assertEqual(result.outcome, 'unknown')
                self.assertFalse(self.bridge._idle)

    async def test_duplicate_and_late_status_cannot_replace_first_terminal_evidence(self):
        for first, second, outcome in (('denied', 'delivered', 'unavailable'), ('delivered', 'refused', 'accepted')):
            with self.subTest(first=first):
                frame, task = await self.begin()
                await self.status(frame.message_id, first)
                await self.status(frame.message_id, second)
                self.assertEqual((await task).outcome, outcome)
                self.assertEqual((await self.bridge.deliver(frame)).outcome, outcome)
        frame, task = await self.begin()
        self.assertEqual((await task).outcome, 'unknown')
        await self.status(frame.message_id, 'delivered')
        self.assertEqual((await self.bridge.deliver(frame)).outcome, 'unknown')

    async def test_silent_enqueue_drain_and_idle_never_confirm_user_acceptance(self):
        frame, task = await self.begin(True)
        await self.callback(action='peer_idle_notice', orig_msg_id=str(frame.message_id), state='idle')
        self.assertEqual((await task).outcome, 'unknown')
        self.assertFalse(await self.bridge.wait_idle(frame.message_id, timeout=.01))
        self.assertTrue(self.frames.empty())

    async def test_dropped_list_only_settles_exact_pending_user_ids(self):
        one, first = await self.begin()
        two, second = await self.begin()
        three, third = await self.begin()
        await self.status(uuid4(), 'dropped', drop_reason='queue-full',
            dropped_msg_ids=[str(one.message_id), str(two.message_id), str(one.message_id), str(uuid4())])
        self.assertEqual((await first).outcome, 'unavailable')
        self.assertEqual((await second).outcome, 'unavailable')
        self.assertFalse(third.done())
        await self.status(three.message_id, 'delivered')
        self.assertEqual((await third).outcome, 'accepted')
        for reason in ('rate-limited', 'duplicate', 'hop-loop', 'hop-runaway', 'queue-full'):
            frame, task = await self.begin()
            await self.status(frame.message_id, 'dropped', drop_reason=reason)
            self.assertEqual((await task).outcome, 'unavailable')

    async def test_drop_ids_are_bounded_and_not_used_by_other_statuses(self):
        for invalid in ('not-a-list', [None], [7], ['x'*257], ['x']*256):
            with self.subTest(ids_type=type(invalid).__name__, count=len(invalid)):
                frame, task = await self.begin()
                ids = ([str(frame.message_id), *invalid] if type(invalid) is list else invalid)
                await self.status(uuid4(), 'dropped', dropped_msg_ids=ids)
                self.assertEqual((await task).outcome, 'unknown')
        frame, task = await self.begin()
        await self.status(uuid4(), 'dropped', dropped_msg_ids=[str(frame.message_id), *(['x']*255)])
        self.assertEqual((await task).outcome, 'unavailable')
        frame, task = await self.begin()
        await self.status(uuid4(), 'delivered', dropped_msg_ids=[str(frame.message_id)])
        self.assertEqual((await task).outcome, 'unknown')

    async def test_idle_requires_exact_subscription_and_idle_state(self):
        for state in ('busy', 'held', 'exited', 'unavailable', 'unknown', '', None, 1):
            with self.subTest(state=state):
                frame, subscription = await self.subscribe()
                await self.callback(action='peer_idle_notice', orig_msg_id=subscription, state=state)
                self.assertFalse(await self.bridge.wait_idle(frame.message_id, timeout=.01))
        frame, subscription = await self.subscribe()
        await self.callback(action='peer_idle_notice', orig_msg_id=str(frame.message_id), state='idle')
        await self.callback(action='peer_idle_notice', orig_msg_id=str(uuid4()), state='idle')
        await self.status(subscription, 'delivered')
        self.assertFalse(await self.bridge.wait_idle(frame.message_id, timeout=.01))
        await self.callback(action='peer_idle_notice', orig_msg_id=subscription, state='idle')
        self.assertTrue(await self.bridge.wait_idle(frame.message_id, timeout=.01))
        self.assertFalse(await self.bridge.wait_idle(frame.message_id, timeout=.01))
        await self.callback(action='peer_idle_notice', orig_msg_id=subscription, state='idle')
        self.assertFalse(await self.bridge.wait_idle(frame.message_id, timeout=.01))

    async def test_terminal_subscription_notice_cannot_be_replaced_by_idle(self):
        for state in ('exited', 'unavailable'):
            frame, subscription = await self.subscribe()
            await self.callback(action='peer_idle_notice', orig_msg_id=subscription, state=state)
            await self.callback(action='peer_idle_notice', orig_msg_id=subscription, state='idle')
            self.assertFalse(await self.bridge.wait_idle(frame.message_id, timeout=.01))
        frame, subscription = await self.subscribe()
        await self.callback(action='peer_idle_notice', orig_msg_id=subscription, state='busy')
        self.assertFalse(await self.bridge.wait_idle(frame.message_id, timeout=.01))
        await self.callback(action='peer_idle_notice', orig_msg_id=subscription, state='idle')
        self.assertTrue(await self.bridge.wait_idle(frame.message_id, timeout=.01))

    async def test_malformed_envelopes_fail_closed_without_consuming_pending(self):
        frame, task = await self.begin()
        base = dict(msgV=1, type='control', action='peer_message_status', orig_msg_id=str(frame.message_id), status='delivered')
        for change in ({'msgV': True}, {'msgV': 1.0}, {'msgV': 2}, {'msgV': None},
                {'type': 'user'}, {'orig_msg_id': None}, {'orig_msg_id': []}, {'status': []}):
            await self.callback(raw=(json.dumps({**base, **change})+'\n').encode())
            self.assertFalse(task.done())
        await self.status(frame.message_id, 'delivered')
        self.assertEqual((await task).outcome, 'accepted')

    async def test_nonfinite_json_is_not_native_receipt_evidence(self):
        frame, task = await self.begin()
        await self.callback(raw=('{"msgV":1,"type":"control","action":"peer_message_status",'
            '"orig_msg_id":'+json.dumps(str(frame.message_id))+',"status":"delivered","detail":NaN}\n').encode())
        self.assertEqual((await task).outcome, 'unknown')

    async def test_unversioned_idle_and_oversized_callbacks_fail_closed(self):
        frame, subscription = await self.subscribe()
        await self.callback(raw=(json.dumps(dict(type='control', action='peer_idle_notice',
            orig_msg_id=subscription, state='idle'))+'\n').encode())
        self.assertFalse(await self.bridge.wait_idle(frame.message_id, timeout=.01))
        await self.callback(raw=(json.dumps(dict(msgV=1, type='control', action='peer_idle_notice',
            orig_msg_id=subscription, state='idle', detail='x'*8192))+'\n').encode())
        self.assertFalse(await self.bridge.wait_idle(frame.message_id, timeout=.01))
        await self.callback(action='peer_idle_notice', orig_msg_id=subscription, state='idle')
        self.assertTrue(await self.bridge.wait_idle(frame.message_id, timeout=.01))

    async def test_duplicate_json_fields_fail_closed(self):
        frame, task = await self.begin()
        await self.callback(raw=('{"msgV":1,"type":"control","action":"message_refused",'
            '"action":"peer_message_status","orig_msg_id":'+json.dumps(str(frame.message_id))+
            ',"status":"refused","status":"delivered"}\n').encode())
        self.assertEqual((await task).outcome, 'unknown')

    async def test_cancellation_never_replays_and_close_cleans_silent_callbacks(self):
        frame, task = await self.begin()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.status(frame.message_id, 'delivered')
        self.assertEqual((await self.bridge.deliver(frame)).outcome, 'unknown')
        self.assertTrue(self.frames.empty())
        reader, writer = await asyncio.open_unix_connection(str(self.bridge._callback))
        async def accepted():
            while not self.bridge._tasks:
                await asyncio.sleep(0)
        await asyncio.wait_for(accepted(), 1)
        await self.bridge.aclose()
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
        writer.close()
        await writer.wait_closed()

    async def test_pending_and_replay_memory_are_bounded(self):
        frames = [self.frame() for _ in range(64)]
        tasks = [asyncio.create_task(self.bridge.deliver(frame)) for frame in frames]
        try:
            for _ in frames:
                await asyncio.wait_for(self.frames.get(), 1)
            self.assertLessEqual(len(self.bridge._pending), 64)
            with self.assertRaisesRegex(ConnectionError, 'capacity'):
                await self.bridge.deliver(self.frame())
            results = await asyncio.gather(*tasks)
            self.assertEqual({r.outcome for r in results}, {'unknown'})
            self.assertEqual(len(self.bridge._seen), 64)
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
