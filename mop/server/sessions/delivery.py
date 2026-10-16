"""Async private delivery gateway. Root владеет runtime, peers и этим adapter.

Нет broker publish -> delivered. Transport.ready подтверждает подписку только
для attach; ApplicationAcceptance подтверждает сообщение отдельно. Все ORM
sections проходят цельными sync services через owned bounded runtime.
"""
import asyncio
from uuid import uuid4

from mop.common.endpoint import ApplicationAcceptance
from .endpoints import Conflict, Unavailable


class DeliveryGateway:
    def __init__(self, runtime, endpoints, *, connection_limit=16):
        if not 1 <= connection_limit <= 16:
            raise ValueError('private gateway connection limit must be within 1..16')
        self.runtime, self.endpoints = runtime, endpoints
        self.connection_limit = connection_limit
        self.gateway_id = uuid4()
        self._peers = {}
        self._attaching = 0
        self._busy = set()
        self._calls = set()
        self._work = {}
        self._closing = set()
        self._closers = {}
        self._loops = {}
        self._loop_failure = None
        self._idle_pending = {}
        self._stopped = False

    async def start(self):
        if self._stopped:
            raise RuntimeError('private gateway is stopped')

    async def attach(self, actor, endpoint_id, peer):
        return await self.runtime.effect(self._attach, actor, endpoint_id, peer)

    async def _attach(self, actor, endpoint_id, peer):
        if self._stopped or len(self._peers)+self._attaching >= self.connection_limit:
            raise ConnectionError('private gateway connection capacity exhausted')
        self._attaching += 1
        task = asyncio.current_task()
        self._calls.add(task)
        context = None
        try:
            presence = await self.runtime.database(self.endpoints.presence, actor, endpoint_id)
            if presence.external_session_id != peer.external_session_id:
                raise PermissionError('subscription belongs to a different external conversation')
            async with asyncio.timeout(5):
                await peer.ready()
                await peer.start()
            context = await self.runtime.database(self.endpoints.connect, actor, endpoint_id, gateway_id=self.gateway_id)
            if self._stopped:
                raise ConnectionError('private gateway stopped during attach')
            self._peers[context] = peer
            return context
        except BaseException:
            if context is not None:
                await self.runtime.database(self.endpoints.disconnect, context)
            close = getattr(peer, 'aclose', None)
            if close is not None:
                await close()
            raise
        finally:
            self._attaching -= 1
            self._calls.discard(task)

    async def deliver_one(self, context):
        return await self.runtime.effect(self._deliver_one, context)

    async def _deliver_one(self, context):
        if self._stopped or context in self._closing or context not in self._peers:
            raise ConnectionError('private gateway receiver is unavailable')
        if context in self._busy:
            return None
        self._busy.add(context)
        task = asyncio.current_task()
        self._calls.add(task)
        self._work.setdefault(context, set()).add(task)
        try:
            await self.runtime.database(self.endpoints.renew, context)
            frame = await self.runtime.database(self.endpoints.take, context)
            if frame is None:
                return None
            async def prewrite():
                if self._stopped or context in self._closing:
                    raise ConnectionError('private gateway receiver is closing')
                await self.runtime.database(self.endpoints.before_dispatch, context, frame.id)
                if self._stopped or context in self._closing:
                    raise ConnectionError('private gateway receiver is closing')
            await prewrite()
            outcome = 'unknown'
            try:
                async with asyncio.timeout(5):
                    peer = self._peers[context]
                    # Native peer проверяет port после readiness и socket open,
                    # непосредственно перед write; port не входит в wire frame.
                    guarded = getattr(peer, 'deliver_guarded', None)
                    evidence = await guarded(frame, prewrite) if guarded else await peer.deliver(frame)
                if type(evidence) is ApplicationAcceptance and evidence.outcome in ('accepted', 'unknown', 'unavailable'):
                    outcome = evidence.outcome
            except (OSError, TimeoutError):
                # Нет доказанного effect boundary: transport exception не failure.
                outcome = 'unknown'
            # После await снова проверяются credential/client/project/lease fence.
            result = await self.runtime.database(self.endpoints.acknowledge, context, frame.id, outcome=outcome)
            if result.state == 'accepted' and frame.notify_when_idle:
                pending = self._idle_pending.setdefault(context, {})
                if len(pending) >= 64:
                    raise ConnectionError('private idle notification backlog exhausted')
                pending[frame.id] = frame.message_id
            return result
        finally:
            self._busy.discard(context)
            self._calls.discard(task)
            self._work[context].discard(task)
            if not self._work[context]:
                self._work.pop(context)

    async def observe_idle(self, context, delivery_id, message_id, *, timeout=2):
        if context not in self._peers or self._stopped or context in self._closing:
            raise ConnectionError('private gateway receiver is unavailable')
        async def observe():
            task = asyncio.current_task()
            self._calls.add(task)
            self._work.setdefault(context, set()).add(task)
            try:
                if context in self._closing:
                    raise ConnectionError('private gateway receiver is closing')
                expected = await self.runtime.database(self.endpoints.idle_message, context, delivery_id)
                if message_id != expected:
                    raise PermissionError('idle message does not match addressed delivery')
                async with asyncio.timeout(5):
                    idle = await self._peers[context].wait_idle(expected, timeout=timeout)
                if idle:
                    result = await self.runtime.database(self.endpoints.idle, context, delivery_id)
                    self._idle_pending.get(context, {}).pop(delivery_id, None)
                    return result
                await self.runtime.database(self.endpoints.validate, context)
                return None
            finally:
                self._calls.discard(task)
                self._work[context].discard(task)
                if not self._work[context]:
                    self._work.pop(context)
        return await self.runtime.effect(observe)

    async def cycle(self, context):
        """Один bounded tick для WS wakeup/poll loop; не HTTP long polling."""
        return await self.deliver_one(context)

    def start_receiver(self, context):
        """Root-owned loop, один на current connection; HTTP его не ожидает."""
        if self._stopped or context in self._closing or context not in self._peers:
            raise ConnectionError('private gateway receiver is unavailable')
        if context in self._loops:
            return self._loops[context][0]
        stop = asyncio.Event()
        task = asyncio.create_task(self._receive_loop(context, stop))
        # Завершённые tasks не остаются в registry после reconnect. Первая
        # неожиданная ошибка сохраняется до shutdown; task остаётся у caller.
        task.add_done_callback(lambda value: self._receiver_done(context, value))
        self._loops[context] = (task, stop)
        return task

    def _receiver_done(self, context, task):
        self._loops.pop(context, None)
        if not task.cancelled():
            error = task.exception()
            if error is not None and self._loop_failure is None:
                self._loop_failure = error

    async def _receive_loop(self, context, stop):
        try:
            while not stop.is_set() and not self._stopped:
                await self.cycle(context)
                for delivery_id, message_id in tuple(self._idle_pending.get(context, {}).items()):
                    if stop.is_set() or self._stopped:
                        break
                    await self.observe_idle(context, delivery_id, message_id, timeout=.001)
                try:
                    await asyncio.wait_for(stop.wait(), .25)
                except TimeoutError:
                    pass
            return {'state': 'closed', 'reason': 'receiver stopped'}
        except (PermissionError, Conflict, Unavailable) as error:
            # Обычное закрытие после отзыва — наблюдаемый результат подписки.
            return {'state': 'closed', 'reason': str(error)}
        finally:
            await self.detach(context)

    async def detach(self, context):
        # Сначала запрещаем новый write и доказываем quiescence транспорта.
        # Timeout оставляет peer/lease у owner для повторного drain, не clean close.
        peer = self._peers.get(context)
        if peer is None:
            return
        self._closing.add(context)
        pair = self._loops.get(context)
        if pair:
            pair[1].set()
        self._idle_pending.pop(context, None)
        close = self._closers.get(context)
        if close is None:
            close = asyncio.create_task(peer.aclose())
            self._closers[context] = close
        _, pending = await asyncio.wait((close,), timeout=5)
        if pending:
            raise RuntimeError('private delivery detach incomplete: peer cleanup')
        try:
            close.result()
        finally:
            self._closers.pop(context, None)
        tasks = self._work.get(context, set()) - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5)
            if pending:
                raise RuntimeError('private delivery detach incomplete: active transport work')
        await self.runtime.database(self.endpoints.disconnect, context)
        self._peers.pop(context, None)
        self._closing.discard(context)

    async def aclose(self):
        self._stopped = True
        failures = []
        for task, stop in self._loops.values():
            stop.set()
        for context in tuple(self._peers):
            await self.detach(context)
        if self._loops:
            tasks = tuple(pair[0] for pair in self._loops.values())
            _, pending = await asyncio.wait(tasks, timeout=10)
            if pending:
                raise RuntimeError('private delivery shutdown incomplete: receiver loops')
            failures = [task.exception() for task in tasks if not task.cancelled() and task.exception() is not None]
            self._loops.clear()
        if self._calls:
            _, pending = await asyncio.wait(tuple(self._calls), timeout=10)
            if pending:
                raise RuntimeError('private delivery shutdown incomplete: active transport work')
        for context in tuple(self._peers):
            await self.detach(context)
        failure = self._loop_failure or (failures[0] if failures else None)
        self._loop_failure = None
        if failure is not None:
            raise RuntimeError('private receiver loop failed: '+type(failure).__name__) from None
