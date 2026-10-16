"""Узкие event ports: Core NATS — wakeup, не management и не public history."""
import asyncio
import json
import re
from typing import Protocol
from uuid import UUID


class ResyncRequired(ValueError):
    """Уведомления потеряны; новый REST snapshot вместо притворного replay."""


class EventSubscription(Protocol):
    async def get(self): ...
    async def aclose(self): ...


class EventSubscriber(Protocol):
    async def subscribe(self, project_ids) -> EventSubscription: ...


class EventPublisher(Protocol):
    async def publish(self, event): ...


def subject(installation, project):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',installation):
        raise ValueError('invalid realtime installation identity')
    return f'mop.rt.v1.{installation}.project.{UUID(str(project))}.events'


def publication(event):
    identifier=str(UUID(event['event_id']))
    epoch=str(UUID(event['epoch']))
    projects=event.get('project_ids') or (event['project_id'],)
    if not 1<=len(projects)<=16:
        raise ValueError('bounded event project audience required')
    projects=tuple(str(UUID(str(project))) for project in projects)
    return projects,{'schema_version':1,'event_id':identifier,'epoch':epoch}


class BufferedSubscription:
    """Overflow закрывает источник, очищает очередь и будит ожидающего reader."""
    def __init__(self, owner, projects, maximum=32):
        self.owner,self.projects=owner,frozenset(str(x) for x in projects)
        self.queue=asyncio.Queue(maximum)
        self.closed=False
        self.failed=False
        self.resources=[]

    def offer(self, hint=None):
        if self.closed or self.failed:
            return
        try:
            self.queue.put_nowait(hint)
        except asyncio.QueueFull:
            self.fail()

    def fail(self):
        self.failed=True
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(None)

    async def get(self):
        value=await self.queue.get()
        if self.failed or self.closed:
            raise ResyncRequired('event subscription requires a new snapshot')
        return value

    async def aclose(self):
        if self.closed:
            return
        self.closed=True
        self.owner._subscriptions.discard(self)
        try:
            async with asyncio.timeout(2):
                results=await asyncio.gather(*(resource.unsubscribe() for resource in self.resources),
                                             return_exceptions=True)
            if any(isinstance(value,BaseException) for value in results):
                raise ResyncRequired('event subscription cleanup failed')
        finally:
            self.resources.clear()
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(None)


class MemoryEvents:
    """Детерминированный port с теми же capacity/overflow/lifecycle исходами."""
    def __init__(self, *, maximum=32, subscriptions=64):
        if not 1<=maximum<=256 or not 1<=subscriptions<=64:
            raise ValueError('event bounds exceed safety caps')
        self.maximum,self.limit=maximum,subscriptions
        self._subscriptions=set()

    @property
    def active(self):
        return len(self._subscriptions)

    async def start(self):
        pass

    async def subscribe(self, project_ids):
        if not 1<=len(project_ids)<=16 or self.active>=self.limit:
            raise ResyncRequired('event subscription capacity exhausted')
        sub=BufferedSubscription(self,project_ids,self.maximum)
        self._subscriptions.add(sub)
        return sub

    async def publish(self, event):
        projects,_=publication(event)
        for sub in tuple(self._subscriptions):
            if sub.projects.intersection(str(x) for x in projects):
                sub.offer()

    async def aclose(self):
        for sub in tuple(self._subscriptions):
            await sub.aclose()


class NatsEvents(MemoryEvents):
    """Одна bounded connection процесса; subscriptions принадлежат соединениям WS.

    Никаких request/reply/queue group: каждый gateway получает wakeup. При
    disconnect/reconnect старый handshake недействителен; Core NATS не durable.
    """
    def __init__(self, *, installation, servers, user=None, password=None, tls=None, **bounds):
        super().__init__(**bounds)
        subject(installation,UUID(int=0))
        self.installation,self.servers,self.user,self.password,self.tls=installation,servers,user,password,tls
        self.connection=None
        self._fault=0

    async def start(self):
        if self.connection is not None:
            if self.connection.is_connected:
                return
            raise ResyncRequired('event broker is not connected')
        from nats.aio.client import Client
        async def broken(*args):
            self._fault+=1
            for sub in tuple(self._subscriptions):
                sub.fail()
        # nats-py пишет PING напрямую, а SUB/PUB обычно кладёт в pending.
        # Forced drain каждого command ставит его на wire ДО receipt PING;
        # иначе flush мог подтвердить PING ещё до broker ACL refusal на SUB.
        self.connection=Client()
        try:
            await self.connection.connect(servers=self.servers,user=self.user,password=self.password,tls=self.tls,
                connect_timeout=2,max_reconnect_attempts=2,reconnect_time_wait=.25,pending_size=1,flush_timeout=2,
                disconnected_cb=broken,reconnected_cb=broken,closed_cb=broken,error_cb=broken)
        except BaseException:
            await self.aclose()
            raise

    async def subscribe(self, project_ids):
        if self.connection is None or not self.connection.is_connected:
            raise ResyncRequired('event broker unavailable')
        sub=await super().subscribe(project_ids)
        async def hint(message):
            # Payload не становится публичным событием: история и scope — SQL.
            if len(message.data)>16384:
                sub.fail()
            else:
                sub.offer()
        try:
            for project in project_ids:
                resource=await self.connection.subscribe(subject(self.installation,project),cb=hint,
                    pending_msgs_limit=self.maximum,pending_bytes_limit=65536)
                sub.resources.append(resource)
            await self.connection.flush(timeout=2)
            if sub.failed:
                raise ResyncRequired('event subscription was refused or interrupted')
            return sub
        except BaseException:
            await sub.aclose()
            raise

    async def publish(self, event):
        if self.connection is None or not self.connection.is_connected:
            raise ResyncRequired('event broker unavailable')
        # Совместим с immutable event словарём существующего #625 publisher.
        # Ни code, ни raw target metadata, ни terminal bytes брокеру не нужны.
        projects,metadata=publication(event)
        payload=json.dumps(metadata,separators=(',',':')).encode()
        generation=self._fault
        for project in projects:
            await self.connection.publish(subject(self.installation,project),payload)
        # Это только broker receipt. Не ACK доставки endpoint/живой сессии.
        await self.connection.flush(timeout=2)
        if generation!=self._fault:
            raise ResyncRequired('event publication was refused or interrupted')

    def blocking_publisher(self, loop):
        """Callable для существующего sync #625 publish_one, без второй очереди.

        Вызывается в owned blocking worker вне transaction; timeout не выдаётся
        за отказ доставки. Outbox сохраняет row и повторит тот же event_id.
        """
        def publish(event):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise RuntimeError('sync outbox publisher cannot run on the event loop')
            future=asyncio.run_coroutine_threadsafe(self.publish(event),loop)
            try:
                future.result(timeout=3)
            except BaseException:
                future.cancel()
                raise ResyncRequired('event publication receipt is unknown') from None
        return publish

    async def aclose(self):
        await super().aclose()
        if self.connection is not None:
            await self.connection.close()
            self.connection=None


class UnboundSubscriber:
    async def subscribe(self, project_ids):
        raise ResyncRequired('internal event subscriber is not bound')

    async def aclose(self):
        pass


class ProjectionHistory:
    """Adapter actual #626 PoolQueries.events_after; cursor остаётся его authority.

    Source сейчас умеет explicit один project или весь actor scope. Произвольный
    subset не выдаётся за поддержанный vector: до публичного multi-scope query
    gateway требует project-specific REST snapshot для каждого такого socket.
    """
    def __init__(self, queries):
        self.queries=queries

    def after(self, actor, project_ids, cursor):
        if len(project_ids)!=1:
            raise ResyncRequired('select a project-specific snapshot')
        try:
            return self.queries.events_after(actor,cursor,project_id=project_ids[0],limit=100)
        except ValueError:
            raise ResyncRequired('snapshot cursor requires a new authorized snapshot') from None


class UnboundHistory:
    def after(self, actor, project_ids, cursor):
        raise ResyncRequired('snapshot/event history binding is not ready')
