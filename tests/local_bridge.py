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
from unittest.mock import patch

from contextlib import contextmanager


@contextmanager
def changed_cwd(path):
    """Настоящая смена cwd с обязательным восстановлением процесса теста."""
    previous = os.open('.', os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.chdir(path)
        yield
    finally:
        os.fchdir(previous)
        os.close(previous)

import hermetic  # noqa: F401
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mop.client.local_bridge import LocalBridge, OwnSession
from mop.common.endpoint import DeliveryFrame
from _own_session_fixture import own_session


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
        self.bridge = LocalBridge(own_session(self.path, 'own-conversation'), timeout=.08)
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

    # HYPOTHESIS: #735 равные PID/path до await guard не защищают physical
    # scope после него; /clear меняет conversation без смены процесса/сокета.
    # SPEC: connected SO_PEERCRED + полный process proof после последнего await;
    # user и idle control несут точный native session_id. Никакой file authority.
    # RED: 8 behavioral failures: bytes после start/namespace/uid/cwd barrier,
    # stale callback принят, отсутствуют полный proof и native frame fence.
    # SOLUTION: обязательный frozen OwnSession с process_start/uid/namespaces/cwd;
    # stdlib native_process на connected peer после guard/read, без await до write.
    # Второй RED: guard проглотил CancelledError и write продолжился. Последний
    # gate сохраняет task.cancelling(): cleanup guard не возвращает authority.
    # RESULT: все прежние #729 statuses/idle/replay/bounds/cleanup сохранены;
    # /clear проверен captured frames, start/namespace/uid — инъекцией snapshot,
    # cwd — настоящим chdir; это не реальный kernel PID reuse и не native green.
    # STATUS: FIXED — see #735 (client boundary; production root не включён).
    async def test_partial_or_untyped_own_session_is_not_an_authority_fallback(self):
        import dataclasses
        from mop import session
        fields = {f.name for f in dataclasses.fields(OwnSession)}
        self.assertTrue({'process_start', 'uid', 'namespaces', 'cwd'} <= fields,
            'trusted launcher must supply complete physical proof, not only PID/path')
        with self.assertRaises(TypeError):
            OwnSession(self.path, os.getpid(), 'own-conversation')
        snapshot = session.native_process(os.getpid())
        values = dict(socket_path=self.path, pid=os.getpid(), external_session_id='own-conversation',
            process_start=snapshot['start'], uid=snapshot['uid'], namespaces=snapshot['namespaces'],
            cwd=Path(snapshot['cwd']))
        for field, invalid in (('pid', True), ('uid', True), ('process_start', ''),
                ('namespaces', ()), ('namespaces', dict(snapshot['namespaces'])),
                ('cwd', Path('relative')), ('socket_path', 'relative.sock'),
                ('external_session_id', ''), ('external_session_id', 'x'*129)):
            with self.subTest(field=field, invalid=invalid):
                with self.assertRaises((ValueError, TypeError)):
                    LocalBridge(OwnSession(**{**values, field: invalid}))
        with self.assertRaises(ValueError):
            LocalBridge(values)
        with self.assertRaises(ValueError):
            LocalBridge(OwnSession(**values), timeout=5.001)

    async def test_connected_peer_is_checked_after_guard_and_without_another_await(self):
        guarded = False
        checks = []
        original = self.bridge._peer
        def checked(writer):
            checks.append((guarded, writer.get_extra_info('socket').fileno()))
            return original(writer)
        async def guard():
            nonlocal guarded
            await asyncio.sleep(0)
            guarded = True
        with patch.object(self.bridge, '_peer', checked):
            task = asyncio.create_task(self.bridge.deliver_guarded(self.frame(), guard))
            await asyncio.wait_for(self.frames.get(), 1)
            self.assertTrue(any(after for after, _ in checks), 'no connected peer check after guard')
            self.assertEqual((await task).outcome, 'unknown')

    async def test_user_and_idle_control_pin_exact_native_conversation_after_clear(self):
        frame = self.frame(True)
        task = asyncio.create_task(self.bridge.deliver(frame))
        user = await asyncio.wait_for(self.frames.get(), 1)
        self.assertEqual(user.get('session_id'), 'own-conversation')
        await self.status(frame.message_id, 'delivered')
        self.assertEqual((await task).outcome, 'accepted')
        control = await asyncio.wait_for(self.frames.get(), 1)
        self.assertEqual(control.get('session_id'), 'own-conversation')
        # Модель dispatcher для /clear: тот же PID/start/socket, новый ID.
        # Это captured-frame assertion, не эксперимент над native dispatcher.
        current_native_id = 'conversation-after-clear'
        self.assertFalse(any(wire.get('session_id') == current_native_id for wire in (user, control)))
        fresh = self.frame()
        task = asyncio.create_task(self.bridge.deliver(fresh))
        wire = await asyncio.wait_for(self.frames.get(), 1)
        self.assertEqual(wire.get('session_id'), 'own-conversation')
        self.assertNotEqual(wire['session_id'], current_native_id)
        self.assertEqual((await task).outcome, 'unknown')

    async def test_real_cwd_change_while_guard_waits_blocks_all_bytes(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def guard():
            entered.set()
            await release.wait()
        task = asyncio.create_task(self.bridge.deliver_guarded(self.frame(), guard))
        await asyncio.wait_for(entered.wait(), 1)
        try:
            with changed_cwd(self.directory.name):
                release.set()
                result = await task
            self.assertEqual(result.outcome, 'unknown')
            self.assertTrue(self.frames.empty(), 'post-guard cwd changed but plaintext escaped')
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_process_start_and_namespaces_rechecked_after_guard_snapshot_barrier(self):
        from mop import session
        original = session.native_process
        for field in ('start', 'namespaces', 'uid'):
            with self.subTest(field=field):
                entered, release = asyncio.Event(), asyncio.Event()
                async def guard():
                    entered.set()
                    await release.wait()
                task = asyncio.create_task(self.bridge.deliver_guarded(self.frame(), guard))
                await asyncio.wait_for(entered.wait(), 1)
                def changed(pid):
                    result = original(pid)
                    if field == 'start':
                        # Инъекция read snapshot, не настоящий kernel PID reuse.
                        result['start'] += '1'
                    elif field == 'uid':
                        result['uid'] += 1
                    else:
                        result['namespaces'] = tuple((name, inode+1) for name, inode in result['namespaces'])
                    return result
                with patch.object(session, 'native_process', changed):
                    release.set()
                    self.assertEqual((await task).outcome, 'unknown')
                self.assertTrue(self.frames.empty(), f'post-guard {field} changed but bytes escaped')

    async def test_callback_rechecks_current_scope_after_waiting_for_receipt_bytes(self):
        frame, task = await self.begin()
        entered = asyncio.Event()
        original = self.bridge._peer
        def checked(writer):
            result = original(writer)
            entered.set()
            return result
        with patch.object(self.bridge, '_peer', checked):
            reader, writer = await asyncio.open_unix_connection(str(self.bridge._callback))
            await asyncio.wait_for(entered.wait(), 1)
            try:
                with changed_cwd(self.directory.name):
                    writer.write((json.dumps(dict(msgV=1, type='control', action='peer_message_status',
                        orig_msg_id=str(frame.message_id), status='delivered'))+'\n').encode())
                    await writer.drain()
                    writer.write_eof()
                    await asyncio.wait_for(reader.read(), 1)
            finally:
                writer.close()
                await writer.wait_closed()
        self.assertEqual((await task).outcome, 'unknown', 'pre-read peer snapshot accepted stale callback')

    async def test_guard_suppressing_cancellation_cannot_resume_a_cancelled_write(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def guard():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Trusted port может закончить свою cleanup после cancel;
                # это не разрешение bridge продолжить начатый write.
                await release.wait()
        frame = self.frame()
        task = asyncio.create_task(self.bridge.deliver_guarded(frame, guard))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.frames.empty(), 'cancelled guard resumed and emitted bytes')
        self.assertFalse(self.bridge._writers)
        self.assertFalse(self.bridge._pending)
        self.assertEqual((await self.bridge.deliver(frame)).outcome, 'unknown')

    async def test_guard_close_cancel_and_timeout_never_emit_late_bytes(self):
        for action in ('cancel', 'timeout', 'close'):
            with self.subTest(action=action):
                entered, release = asyncio.Event(), asyncio.Event()
                async def guard():
                    entered.set()
                    await release.wait()
                frame = self.frame()
                task = asyncio.create_task(self.bridge.deliver_guarded(frame, guard))
                await asyncio.wait_for(entered.wait(), 1)
                if action == 'cancel':
                    task.cancel()
                elif action == 'close':
                    await self.bridge.aclose()
                if action in ('cancel', 'close'):
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    self.assertEqual((await task).outcome, 'unknown')
                release.set()
                self.assertTrue(self.frames.empty())
                self.assertFalse(self.bridge._pending)
                self.assertFalse(self.bridge._outbound)
                self.assertFalse(self.bridge._writers)
                if action != 'close':
                    self.assertEqual((await self.bridge.deliver(frame)).outcome, 'unknown')

    async def test_silence_deadline_cleans_pending_without_replaying(self):
        frame, task = await self.begin()
        self.assertEqual(set(self.bridge._pending), {str(frame.message_id)})
        self.assertEqual((await asyncio.wait_for(task, 1)).outcome, 'unknown')
        self.assertFalse(self.bridge._pending)
        self.assertFalse(self.bridge._outbound)
        self.assertFalse(self.bridge._writers)
        self.assertEqual(len(self.bridge._seen), 1)
        await self.status(frame.message_id, 'delivered')
        self.assertEqual((await self.bridge.deliver(frame)).outcome, 'unknown')
        self.assertTrue(self.frames.empty(), 'silence deadline must not replay a possible write')

    # HYPOTHESIS: #737 короткий silence budget fixture истекает ещё в ready/write
    # при 64 полных physical proofs; очередь путает отсутствие bytes с утечкой.
    # RED: PYTHONTRACEMALLOC=1, настоящий Unix peer: ready:unavailable=64,
    # делегирующий observer ready поймал TimeoutError=64, не physical refusal.
    # Без tracing общий guard barrier пересекает прежние 80 ms deadlines:
    # guard:unknown=64, received=0, pending/writers/outbound/callbacks=0,
    # seen=64, cancel=0. Детерминированный RED даже на быстрой машине.
    # SOLUTION: capacity — штатный default budget и общий prewrite barrier;
    # короткий silence deadline проверяется отдельно, не 64-way latency SLA.
    # RESULT: 24 bridge checks, 6 strict файлов и actual PostgreSQL endpoint
    # gate 45 + cross-UID 3 green. Capacity прошла также с tracing; весь traced
    # suite не green из-за отдельного malformed-envelope check. Production
    # fences/bounds не изменены; Unix fixture не равна native acceptance.
    # STATUS: FIXED — see #737
    async def test_pending_and_replay_memory_are_bounded(self):
        silence_budget = self.bridge.timeout
        await self.bridge.aclose()
        self.bridge = LocalBridge(own_session(self.path, 'own-conversation'))
        await self.bridge.start()
        loop = asyncio.get_running_loop()
        started = loop.time()
        phases = {}
        entered, release = asyncio.Event(), asyncio.Event()
        frames = [self.frame() for _ in range(64)]
        ids = {str(frame.message_id) for frame in frames}
        async def deliver(frame):
            async def guard():
                phases[frame.message_id] = 'guard'
                if sum(phase == 'guard' for phase in phases.values()) == 64:
                    entered.set()
                await release.wait()
            phases[frame.message_id] = 'ready'
            try:
                result = await self.bridge.deliver_guarded(frame, guard)
                phases[frame.message_id] += ':'+result.outcome
                return result
            except BaseException as error:
                phases[frame.message_id] += ':'+type(error).__name__
                raise
        tasks = [asyncio.create_task(deliver(frame)) for frame in frames]
        received = set()
        def diagnostic():
            from collections import Counter
            return (f'elapsed={loop.time()-started:.3f}, timeout={self.bridge.timeout}, '
                f'phases={dict(Counter(phases.values()))}, received={len(received)}, '
                f'pending={len(self.bridge._pending)}, seen={len(self.bridge._seen)}, '
                f'outbound={len(self.bridge._outbound)}, writers={len(self.bridge._writers)}, '
                f'callbacks={len(self.bridge._tasks)}, '
                f'tasks_done={sum(task.done() for task in tasks)}, '
                f'tasks_cancelled={sum(task.cancelled() for task in tasks)}')
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(set(self.bridge._pending), ids)
            self.assertEqual(len(self.bridge._seen), 64)
            self.assertEqual(len(self.bridge._outbound), 64)
            self.assertEqual(len(self.bridge._writers), 64)
            self.assertTrue(all(not task.done() for task in tasks))
            self.assertTrue(self.frames.empty(), 'prewrite barrier must hold every user frame')
            with self.assertRaisesRegex(ConnectionError, 'capacity'):
                await self.bridge.deliver(self.frame())
            self.assertEqual(set(self.bridge._pending), ids)
            # Барьер намеренно пересекает короткий silence budget: capacity
            # должна наблюдаться без предположения «64 proofs быстрее 80 ms».
            # До этого timer все 64 _write deadlines уже запущены: прежняя
            # fixture гарантированно теряет pending даже на быстрой машине.
            boundary = loop.create_future()
            timer = loop.call_later(silence_budget, boundary.set_result, None)
            try:
                await boundary
            finally:
                timer.cancel()
            self.assertEqual(set(self.bridge._pending), ids, diagnostic())
            self.assertEqual(set(phases.values()), {'guard'}, diagnostic())
            release.set()
            for _ in frames:
                wire = await asyncio.wait_for(self.frames.get(), 1)
                self.assertEqual(wire['type'], 'user')
                self.assertEqual(wire['session_id'], 'own-conversation')
                self.assertIn(wire['msg_id'], ids)
                self.assertNotIn(wire['msg_id'], received)
                received.add(wire['msg_id'])
            self.assertEqual(received, ids)
            self.assertEqual(set(self.bridge._pending), ids)
            # Один настоящий native terminal receipt завершает точные 64 IDs;
            # unknown по таймеру не доказывает, что capacity вообще достигнута.
            await self.status(uuid4(), 'dropped', dropped_msg_ids=sorted(ids))
            results = await asyncio.wait_for(asyncio.gather(*tasks), 1)
            self.assertEqual({r.outcome for r in results}, {'unavailable'})
            self.assertFalse(self.bridge._pending)
            self.assertFalse(self.bridge._outbound)
            self.assertFalse(self.bridge._writers)
            self.assertEqual(len(self.bridge._seen), 64)
            for frame in frames:
                self.assertEqual((await self.bridge.deliver(frame)).outcome, 'unavailable')
            with self.assertRaisesRegex(ConnectionError, 'capacity'):
                await self.bridge.deliver(self.frame())
            self.assertEqual(len(self.bridge._seen), 64)
            self.assertTrue(self.frames.empty(), 'capacity refusal and replay must emit no bytes')
        except TimeoutError:
            self.fail('capacity fixture did not reach its controlled phase: '+diagnostic())
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
