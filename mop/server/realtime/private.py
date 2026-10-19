"""Конечный private WS peer; native proof остаётся у доверенного LocalBridge.

JSON утверждает только protocol evidence этого пира. Здесь нет native process
resolver, human launcher, RPC или нового владельца durable receipts.
"""
import asyncio
from dataclasses import asdict
from uuid import UUID, uuid4

from mop.common.credentials import Credential
from mop.common.endpoint import ApplicationAcceptance, inbox_decode, inbox_encode
from mop.server.access.services import Denied, NotVisible
from mop.server.controller.runtime import Backpressure
from mop.server.sessions.endpoints import Conflict, Unavailable
from .gateway import OwnedConsumer


def _wire_value(value):
    return {key: str(item) if type(item) is UUID else item for key, item in asdict(value).items()}


class InboxPeer:
    """Один outstanding delivery/probe; dispatcher устанавливается до attach."""
    def __init__(self, consumer, external_session_id):
        self.consumer = consumer
        self.external_session_id = external_session_id
        loop = asyncio.get_running_loop()
        self.initial = loop.create_future()
        self.confirmed = loop.create_future()
        self.probe = None
        self.attempt = None
        self.receipts = {}
        self.idle = set()
        self.closed = False
        self.ready_at = 0

    async def ready(self):
        await self.initial
        return self.external_session_id

    async def start(self):
        if self.closed or not self.initial.done():
            raise ConnectionError('private receiver dispatcher is not ready')

    def stop(self):
        self.closed = True
        for future in (self.initial, self.confirmed, self.probe[1] if self.probe else None,
                       *(self.attempt['futures'] if self.attempt else ())):
            if future is not None and not future.done():
                future.cancel()

    async def aclose(self):
        self.stop()

    async def probe_ready(self):
        if self.closed or self.probe is not None:
            raise ConnectionError('private receiver probe unavailable')
        identifier = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self.probe = (identifier, future)
        try:
            async with asyncio.timeout(2):
                await self.consumer.emit('probe', probe_id=identifier)
                await future
            self.ready_at = asyncio.get_running_loop().time()
        finally:
            self.probe = None
            if not future.done():
                future.cancel()

    async def deliver_guarded(self, frame, guard):
        if self.closed or self.attempt is not None or len(self.receipts) >= 64:
            raise ConnectionError('private receiver delivery capacity exhausted')
        loop = asyncio.get_running_loop()
        prewrite, acceptance = loop.create_future(), loop.create_future()
        attempt = dict(id=str(frame.id), message_id=str(frame.message_id), futures=(prewrite, acceptance),
                       notify=frame.notify_when_idle, phase='delivery')
        self.attempt = attempt
        try:
            await self.consumer.recheck()
            await guard()
            await self.consumer.emit('delivery', frame=_wire_value(frame))
            done, _ = await asyncio.wait((prewrite, acceptance), return_when=asyncio.FIRST_COMPLETED)
            if acceptance not in done:
                await self.consumer.recheck()
                await guard()
                if self.closed:
                    raise ConnectionError('private receiver is closing')
                attempt['phase'] = 'permitted'
                await self.consumer.emit('write_allowed', delivery_id=attempt['id'], message_id=attempt['message_id'])
            evidence = await acceptance
            return ApplicationAcceptance(evidence)
        finally:
            attempt['phase'] = 'ended'
            for future in (prewrite, acceptance):
                if not future.done():
                    future.cancel()

    def commit(self, result):
        attempt = self.attempt
        if attempt is None or str(result.id) != attempt['id']:
            raise Conflict('private receipt does not match outstanding attempt')
        self.receipts[attempt['id']] = dict(result=result, message_id=attempt['message_id'], notify=attempt['notify'])
        self.attempt = None

    async def wait_idle(self, message_id, *, timeout):
        return str(message_id) in self.idle

    def receive(self, value):
        kind = value['type']
        owner = self.consumer
        if self.closed:
            raise ValueError('private receiver is closing')
        if kind == 'peer_ready':
            if self.initial.done() or value['external_session_id'] != self.external_session_id:
                raise ValueError('private receiver conversation or handshake changed')
            self.ready_at = asyncio.get_running_loop().time()
            self.initial.set_result(None)
            return
        context = owner.context
        if context is None or value.get('connection_nonce') != str(context.nonce):
            raise ValueError('private receiver connection fence mismatch')
        if kind == 'bound_received':
            if not owner.bound_sent or self.confirmed.done():
                raise ValueError('private receiver handshake out of order')
            self.confirmed.set_result(None)
            return
        if not self.confirmed.done() or self.confirmed.cancelled():
            raise ValueError('private receiver handshake incomplete')
        if kind == 'peer_ready_again':
            if (self.probe is None or self.probe[1].done() or value['probe_id'] != self.probe[0]
                    or value['external_session_id'] != self.external_session_id):
                raise ValueError('private receiver probe mismatch')
            self.probe[1].set_result(None)
            return
        delivery_id, message_id = value['delivery_id'], value['message_id']
        stored = self.receipts.get(delivery_id)
        if stored is not None:
            if stored['message_id'] != message_id:
                raise ValueError('private receiver message mismatch')
            result = stored['result']
            if kind == 'application_acceptance':
                if value['outcome'] != result.state:
                    raise Conflict('private delivery evidence conflicts with committed receipt')
                owner.controls.put_nowait(('receipt', result))
            elif kind == 'idle_observed' and stored['notify'] and result.state == 'accepted':
                if message_id not in self.idle:
                    self.idle.add(message_id)
                    owner.controls.put_nowait(('idle', (UUID(delivery_id), UUID(message_id))))
                elif result.idle:
                    owner.controls.put_nowait(('receipt', result))
            else:
                raise ValueError('private receiver evidence out of order')
            return
        attempt = self.attempt
        if (attempt is None or (delivery_id, message_id) != (attempt['id'], attempt['message_id'])
                or attempt['phase'] == 'ended'):
            raise ValueError('private receiver has no matching outstanding delivery')
        prewrite, acceptance = attempt['futures']
        if kind == 'prewrite' and attempt['phase'] == 'delivery' and not prewrite.done():
            prewrite.set_result(None)
        elif kind == 'application_acceptance' and not acceptance.done():
            if value['outcome'] == 'accepted' and attempt['phase'] != 'permitted':
                raise ValueError('private acceptance requires the current write boundary')
            acceptance.set_result(value['outcome'])
        else:
            raise ValueError('private receiver evidence out of order')


class PrivateInbox(OwnedConsumer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.context = None
        self.peer = None
        self.receiver = None
        self.bound_sent = False
        self.controls = asyncio.Queue(maxsize=16)
        self._closing = False
        self._senders = 0

    def begin_close(self):
        self._closing = True
        if self.receiver is not None:
            self.receiver.close()
        if self.peer is not None:
            self.peer.stop()

    async def finish(self, code, reason=None):
        self.begin_close()
        await super().finish(code, reason)

    async def disconnect(self, code):
        self.begin_close()
        self.stopped.set()

    def background(self, fn):
        async def guarded():
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except (Denied, NotVisible) as error:
                await self.finish(4403, str(error))
            except Backpressure as error:
                await self.finish(4429, str(error))
            except (Conflict, Unavailable) as error:
                await self.finish(4409, str(error))
            except (TimeoutError, ConnectionError):
                await self.finish(4409, 'private receiver deadline or transport unavailable')
            except Exception:
                await self.finish(1011, 'private receiver failed')
        task = asyncio.create_task(guarded())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def connect(self):
        from .authority import credentials, origin_allowed, AuthenticationRequired
        try:
            self.credential = credentials(self.scope)
            if not origin_allowed(self.scope, self.credential):
                raise Denied('private receiver origin refused')
            self.actor = await self.db(self.gateway.authority.authenticate, self.credential)
            await self.open()
        except AuthenticationRequired:
            await self.finish(4401)
        except (Denied, NotVisible) as error:
            await self.finish(4403, str(error))
        except Backpressure as error:
            await self.finish(4429, str(error))
        except Unavailable as error:
            await self.finish(1013, str(error))
        except (ValueError, KeyError):
            await self.finish(4400, 'invalid private receiver route')
        except Exception:
            await self.finish(1011, 'private receiver authentication unavailable')

    async def open(self):
        if self.scope.get('query_string'):
            raise ValueError('private receiver has no query parameters')
        if self.scope.get('scheme') != 'wss':
            raise Denied('private receiver requires verified WSS')
        identifier = self.scope['path'].removeprefix('/ws/v1/inbox/')
        endpoint_id = UUID(identifier)
        if str(endpoint_id) != identifier:
            raise ValueError('canonical private endpoint UUID required')
        if not self.gateway.private_root.available:
            raise Unavailable('private receiver credential service is not bound')
        self.original = Credential(*self.credential)
        self.endpoint_id = endpoint_id
        presence = await self.db(self.gateway.delivery.endpoints.presence, self.actor, endpoint_id)
        self.peer = InboxPeer(self, presence.external_session_id)
        await self.accept()
        self.accepted = True
        self.background(self.bind_receiver)
        # Авторитет не ждёт ни readiness, ни provider/native I/O.
        self.background(self.authority_monitor)

    async def db(self, function, *args, **kwargs):
        from mop.server.controller.runtime import realtime_database
        return await realtime_database(self.gateway.runtime, function, *args, **kwargs)

    async def recheck(self):
        if self._closing:
            raise ConnectionError('private receiver is closing')
        def current():
            actor = self.gateway.authority.authenticate(self.credential)
            if actor != self.actor:
                raise Denied('private receiver credential generation changed')
            endpoints = self.gateway.delivery.endpoints
            presence = (endpoints.validate(self.context) if self.context is not None
                        else endpoints.presence(actor, self.endpoint_id))
            if presence.external_session_id != self.peer.external_session_id:
                raise Denied('private receiver conversation changed')
            authority = endpoints.authority()
            if not authority.effects_enabled:
                raise Denied('private receiver effects are disabled')
            return str(authority.epoch)
        epoch = await self.db(current)
        if self._closing:
            raise ConnectionError('private receiver is closing')
        return epoch

    async def bind_receiver(self):
        # Receive dispatcher уже работает, пока attach ожидает peer_ready.
        async with asyncio.timeout(5):
            await self.peer.ready()
        self.context = await self.gateway.delivery.attach(self.actor, self.endpoint_id, self.peer)
        if self._closing:
            return
        self.receiver = self.gateway.protected.bind(self.original, self.context)
        epoch = await self.recheck()
        self.bound_sent = True
        await self.emit('bound', endpoint_id=str(self.context.endpoint_id), external_session_id=self.peer.external_session_id,
            epoch=epoch, generation=str(self.context.generation), lease_seconds=15)
        async with asyncio.timeout(2):
            await self.peer.confirmed
        self.background(self.receiver_loop)
        self.background(self.probes)
        self.background(self.control_loop)

    async def emit(self, kind, **fields):
        if self._closing or self.context is None:
            raise ConnectionError('private receiver is closing or unbound')
        wire = inbox_encode(dict(schema_version=1, type=kind, connection_nonce=str(self.context.nonce), **fields))
        if self._senders >= 16:
            raise ConnectionError('private receiver writer capacity exhausted')
        self._senders += 1
        try:
            async with asyncio.timeout(2):
                async with self.send_lock:
                    if self._closing:
                        raise ConnectionError('private receiver is closing')
                    await self.send(text_data=wire)
        finally:
            self._senders -= 1

    async def receive(self, text_data=None, bytes_data=None):
        try:
            if bytes_data is not None:
                raise ValueError('private receiver requires text frames')
            self.peer.receive(inbox_decode(text_data, direction='client'))
        except Conflict as error:
            await self.finish(4409, str(error))
        except (ValueError, KeyError, TypeError, UnicodeError, asyncio.QueueFull):
            await self.finish(4400, 'invalid private receiver protocol')

    async def authority_monitor(self):
        loop = asyncio.get_running_loop()
        next_check = loop.time() + self.gateway.interval
        while not self._closing:
            await asyncio.sleep(max(0, next_check-loop.time()))
            next_check += self.gateway.interval
            await self.recheck()

    async def probes(self):
        while not self._closing:
            await asyncio.sleep(3)
            await self.peer.probe_ready()
            await self.recheck()
            await self.db(self.gateway.delivery.endpoints.renew, self.context)

    async def receiver_loop(self):
        while not self._closing:
            if len(self.peer.receipts) >= 64:
                await self.finish(1000, 'private receiver generation capacity reached')
                return
            if asyncio.get_running_loop().time()-self.peer.ready_at > 5:
                await self.finish(4409, 'private receiver readiness expired')
                return
            result = await self.gateway.delivery.deliver_one(self.context)
            if self._closing:
                return
            if result is not None:
                self.peer.commit(result)
                await self.emit('receipt', result=_wire_value(result))
            await asyncio.sleep(.25)

    async def control_loop(self):
        while not self._closing:
            kind, value = await self.controls.get()
            await self.recheck()
            if kind == 'idle':
                delivery_id, message_id = value
                result = await self.gateway.delivery.observe_idle(self.context, delivery_id, message_id)
                if result is None:
                    raise Conflict('private idle evidence was not observed')
                self.peer.receipts[str(delivery_id)]['result'] = result
                self.peer.idle.discard(str(message_id))
            else:
                result = value
            await self.emit('receipt', result=_wire_value(result))

    async def cleanup(self):
        self.begin_close()
        if self.context is not None:
            await self.gateway.protected.close_receiver(self.context)
            await self.gateway.delivery.detach(self.context)
        self.original = self.credential = None
        if self.peer is not None:
            self.peer.receipts.clear()
            self.peer.idle.clear()
            self.peer.attempt = None
        while not self.controls.empty():
            self.controls.get_nowait()
