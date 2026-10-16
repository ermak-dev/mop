"""Channels adapters: private resources и background tasks имеют WS-владельца."""
import asyncio
import base64
import binascii
import json
from urllib.parse import parse_qs
from uuid import UUID, uuid4
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from mop.server.access.services import Denied, NotVisible
from mop.server.sessions.terminal_control import Terminals, Conflict, Capacity, size
from .authority import Authority, AuthenticationRequired, credentials, origin_allowed
from .events import UnboundSubscriber, ResyncRequired, UnboundHistory
from .terminal import RootTerminalStream, TerminalUnavailable


def envelope(value):
    """Публичный DTO из durable query; произвольный NATS payload сюда не идёт."""
    fields={'schema_version','event_id','epoch','project_id','entity','type'}
    if type(value) is not dict or set(value)!=fields or value['schema_version']!=1:
        raise ResyncRequired('invalid event envelope')
    entity=value['entity']
    if (type(entity) is not dict or set(entity)!={'kind','id','incarnation','revision','deleted'} or
            type(entity['deleted']) is not bool or
            entity['kind'] not in ('puppet','operation','project','node','delivery') or
            type(entity['revision']) is not str or not entity['revision'].isdigit() or
            not 1<=len(entity['revision'])<=20 or type(value['type']) is not str or
            value['type'] not in ('puppet.changed','puppet.deleted','operation.changed','project.changed',
                                  'node.changed','delivery.changed')):
        raise ResyncRequired('invalid event metadata')
    for key in ('event_id','epoch','project_id'):
        UUID(value[key])
    UUID(entity['id'])
    if entity['incarnation'] is not None:
        UUID(entity['incarnation'])
    return value


class Gateway:
    def __init__(self, *, runtime, subscriber=None, history=None, authority=None, terminals=None,
                 streams=None, recheck_interval=5, maximum=64):
        if not 0<recheck_interval<=5 or not 1<=maximum<=64:
            raise ValueError('gateway resource limits exceed safety caps')
        self.runtime=runtime
        self.subscriber=subscriber or UnboundSubscriber()
        self.history=history or UnboundHistory()
        self.authority=authority or Authority()
        self.streams=streams or RootTerminalStream()
        self.terminals=terminals or Terminals(retirement_authority=self.streams)
        # Три секунды idle + не более двух на SQL authority: доступ не остаётся
        # открытым на 5s sleep плюс ещё один полный DB timeout.
        self.interval,self.maximum=min(recheck_interval,3),maximum
        self.run_id=uuid4()
        self.connections=set()
        self.closed=False

    async def start(self):
        if self.closed:
            raise RuntimeError('gateway is closed')
        if hasattr(self.subscriber,'start'):
            try:
                await self.subscriber.start()
            except BaseException:
                await self.subscriber.aclose()
                raise

    @property
    def active(self):
        return len(self.connections)

    async def __call__(self, scope, receive, send):
        if self.closed or self.active>=self.maximum:
            await send({'type':'websocket.close','code':4429})
            return
        path=scope.get('path','')
        consumer=Events if path=='/ws/v1/events' else Terminal if path.startswith('/ws/v1/terminal/') else None
        if consumer is None:
            await send({'type':'websocket.close','code':4404})
            return
        instance=consumer(gateway=self)
        self.connections.add(instance)
        try:
            await instance(scope,receive,send)
        finally:
            self.connections.discard(instance)

    async def aclose(self):
        self.closed=True
        for consumer in tuple(self.connections):
            await consumer.finish(1012)
        if self.connections:
            await asyncio.gather(*(consumer.done.wait() for consumer in tuple(self.connections)))
        await self.subscriber.aclose()
        self.streams.require_retired()


class OwnedConsumer(AsyncJsonWebsocketConsumer):
    invalidation_type='reset_required'
    def __init__(self, *, gateway, **kwargs):
        super().__init__(**kwargs)
        self.gateway=gateway
        self.stopped=asyncio.Event()
        self.done=asyncio.Event()
        self.tasks=set()
        self.accepted=False
        self.actor=None
        self.send_lock=asyncio.Lock()

    async def __call__(self, scope, receive, send):
        dispatch=asyncio.create_task(super().__call__(scope,receive,send))
        stopped=asyncio.create_task(self.stopped.wait())
        try:
            completed,_=await asyncio.wait((dispatch,stopped),return_when=asyncio.FIRST_COMPLETED)
            if dispatch in completed:
                await dispatch
        finally:
            async def shutdown():
                dispatch.cancel()
                stopped.cancel()
                for task in self.tasks:
                    task.cancel()
                await asyncio.gather(dispatch,stopped,*self.tasks,return_exceptions=True)
                try:
                    await self.cleanup()
                finally:
                    self.done.set()
            cleanup=asyncio.create_task(shutdown())
            # Повторная отмена владельца не отменяет уже начатый detach/SQL.
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()

    async def send_json(self, content, close=False):
        async with self.send_lock:
            async with asyncio.timeout(2):
                await super().send_json(content,close=close)

    async def finish(self, code, reason=None):
        if not self.stopped.is_set():
            try:
                async with self.send_lock:
                    async with asyncio.timeout(2):
                        if reason is not None:
                            reason=reason.encode('utf-8')[:120].decode('utf-8',errors='ignore')
                        await self.close(code=code,reason=reason)
            finally:
                self.stopped.set()

    def background(self, fn):
        async def guarded():
            try:
                await fn()
            except (Denied,NotVisible) as exc:
                await self.finish(4403,str(exc))
            except (ResyncRequired,Conflict):
                if self.accepted:
                    await self.send_json({'type':self.invalidation_type,'reason':'authority_or_stream_changed'})
                await self.finish(4409)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Текст внешнего исключения не становится protocol/audit data.
                await self.finish(1011)
        task=asyncio.create_task(guarded())
        self.tasks.add(task)

    async def connect(self):
        try:
            self.credential=credentials(self.scope)
            if not origin_allowed(self.scope,self.credential):
                await self.finish(4403)
                return
            async with asyncio.timeout(2):
                self.actor=await self.gateway.runtime.database(self.gateway.authority.authenticate,self.credential)
            await self.open()
        except AuthenticationRequired:
            await self.finish(4401)
        except (Denied,NotVisible) as exc:
            await self.finish(4403,str(exc))
        except (Conflict,Capacity,ResyncRequired):
            await self.finish(4409)
        except (ValueError,KeyError):
            await self.finish(4400)
        except Exception:
            await self.finish(1011)

    async def disconnect(self, code):
        self.stopped.set()

    async def receive(self, text_data=None, bytes_data=None):
        try:
            if bytes_data is not None or text_data is None or len(text_data)>8192:
                raise ValueError('bounded JSON frames required')
            await self.receive_json(json.loads(text_data))
        except (Denied,NotVisible) as exc:
            await self.finish(4403,str(exc))
        except (Conflict,ResyncRequired):
            if self.accepted:
                await self.send_json({'type':self.invalidation_type,'reason':'authority_or_stream_changed'})
            await self.finish(4409)
        except (ValueError,KeyError,TypeError,binascii.Error):
            await self.finish(4400)
        except Exception:
            await self.finish(1011)

    async def cleanup(self):
        pass


class Events(OwnedConsumer):
    async def open(self):
        self.subscription=None
        self.cursor=None
        self.epoch=None
        self.seen={}
        self.catchup_lock=asyncio.Lock()
        query=parse_qs(self.scope.get('query_string',b'').decode('ascii'),strict_parsing=True)
        if set(query)!={'project_id'} or not 1<=len(query['project_id'])<=16:
            raise ValueError('explicit bounded project audience required')
        self.projects=tuple(UUID(x) for x in query['project_id'])
        if len(set(self.projects))!=len(self.projects):
            raise ValueError('duplicate project audience')
        await self.gateway.runtime.database(self.gateway.authority.authorize,self.actor,'catalog.view_pool',self.projects)
        # Отмена subscribe не теряет частично открытые NATS subscriptions.
        awaitable=asyncio.create_task(self.gateway.subscriber.subscribe(self.projects))
        try:
            self.subscription=await asyncio.shield(awaitable)
        except asyncio.CancelledError:
            self.subscription=await awaitable
            raise
        await self.recheck()
        await self.accept()
        self.accepted=True
        await self.send_json({'type':'ready','schema_version':1,'handshake_complete':False})
        self.background(self.notifications)
        self.background(self.periodic)

    async def recheck(self):
        async with asyncio.timeout(2):
            await self.gateway.runtime.database(self.gateway.authority.current,self.credential,self.actor,self.projects)

    async def receive_json(self, content, **kwargs):
        if (type(content) is not dict or set(content)!={'type','cursor'} or content['type']!='subscribe' or
                type(content['cursor']) is not str or not 1<=len(content['cursor'])<=4096 or self.cursor is not None):
            raise ValueError('only snapshot cursor subscription is accepted')
        await self.recheck()
        self.cursor=content['cursor']
        await self.catchup()

    async def catchup(self):
        async with self.catchup_lock:
            if self.cursor is None:
                return
            value=await self.gateway.runtime.database(self.gateway.history.after,self.actor,self.projects,self.cursor)
            if (type(value) is not dict or value.get('reset_required') or value.get('schema_version')!=1 or
                    type(value.get('cursor')) is not str or not 1<=len(value['cursor'])<=4096):
                raise ResyncRequired('snapshot cursor requires resync')
            events=value['results']
            epoch=value['epoch']
            UUID(epoch)
            if self.epoch not in (None,epoch) or type(events) is not list or len(events)>256:
                raise ResyncRequired('event epoch or catchup bound changed')
            results=[]
            for raw in events:
                item=envelope(raw)
                if UUID(item['project_id']) not in self.projects or item['epoch']!=epoch:
                    raise ResyncRequired('event scope or epoch changed')
                if item['event_id'] not in self.seen:
                    results.append(item)
            await self.recheck()
            # Один atomic WS frame сохраняет actual #626 batch cursor. Не
            # присваивать последнюю позицию первому event: потеря хвоста теряла бы gap.
            coverage=value['coverage']
            if (type(coverage) is not list or not 1<=len(coverage)<=16 or
                    any(type(owner) is not str or not 1<=len(owner)<=64 for owner in coverage)):
                raise ResyncRequired('event publication coverage is not bound')
            # Готов handshake этого stream, а не всех источников установки:
            # coverage владельца нельзя терять или выдавать за полное покрытие.
            frame={'type':'events','schema_version':1,'epoch':epoch,'cursor':value['cursor'],
                   'results':results,'coverage':coverage,'handshake_complete':True}
            if len(json.dumps(frame,separators=(',',':')).encode())>131072:
                raise ResyncRequired('event batch byte bound exceeded')
            await self.send_json(frame)
            self.epoch,self.cursor=epoch,value['cursor']
            for item in results:
                self.seen[item['event_id']]=True
            while len(self.seen)>256:
                self.seen.pop(next(iter(self.seen)))

    async def notifications(self):
        while True:
            await self.subscription.get()
            await self.recheck()
            await self.catchup()

    async def periodic(self):
        while True:
            await asyncio.sleep(self.gateway.interval)
            await self.recheck()
            # Потеря последнего NATS hint обнаруживается не позже 5 секунд.
            await self.catchup()

    async def cleanup(self):
        if getattr(self,'subscription',None) is not None:
            await self.subscription.aclose()


class Terminal(OwnedConsumer):
    invalidation_type='control_lost'
    async def open(self):
        self.context=None
        self.stream=None
        self.output_seq=0
        if self.scope.get('query_string'):
            raise ValueError('terminal credentials and handles are not query parameters')
        attachment_id=UUID(self.scope['path'].removeprefix('/ws/v1/terminal/'))
        reservation=await self.gateway.runtime.database(self.gateway.terminals.inspect,self.actor,attachment_id)
        await self.gateway.streams.preflight(reservation)
        task=asyncio.create_task(self.gateway.runtime.database(self.gateway.terminals.bind,self.actor,uuid4(),
            attachment_id,gateway_id=self.gateway.run_id))
        try:
            self.context=await asyncio.shield(task)
        except asyncio.CancelledError:
            self.context=await task
            raise
        task=asyncio.create_task(self.gateway.streams.open(self.context,current=self.recheck,pin=self.pin_physical))
        try:
            async with asyncio.timeout(5):
                self.stream=await asyncio.shield(task)
        except BaseException:
            task.cancel()
            result=await asyncio.gather(task,return_exceptions=True)
            if result and not isinstance(result[0],BaseException):
                self.stream=result[0]
            raise
        await self.recheck()
        await self.accept()
        self.accepted=True
        await self.send_json({'type':'opened','id':self.context['id'],'incarnation':self.context['incarnation_id'],
            'epoch':str(self.gateway.run_id),'generation':str(self.context['generation']),'mode':self.context['mode']})
        self.background(self.output)
        self.background(self.periodic)

    async def pin_physical(self, instance):
        task=asyncio.create_task(self.gateway.runtime.database(self.gateway.terminals.pin_instance,
            self.actor,self.context,**instance))
        try:
            self.context=await asyncio.shield(task)
        except asyncio.CancelledError:
            self.context=await task
            raise
        return self.context

    async def recheck(self):
        async with asyncio.timeout(2):
            actor=await self.gateway.runtime.database(self.gateway.authority.authenticate,self.credential)
            if actor!=self.actor:
                raise Denied('terminal credential changed')
            await self.gateway.runtime.database(self.gateway.terminals.validate,self.actor,self.context)

    async def output(self):
        while True:
            data=await self.stream.read()
            # Повторная проверка именно после ожидания, до приватных bytes.
            await self.recheck()
            if not data:
                await self.send_json({'type':'closed'})
                await self.finish(1000)
                return
            if type(data) is not bytes or len(data)>4096:
                raise TerminalUnavailable('terminal output exceeded stream bound')
            self.output_seq+=1
            await self.send_json({'type':'output','epoch':str(self.gateway.run_id),
                'incarnation':self.context['incarnation_id'],'seq':self.output_seq,
                'data':base64.b64encode(data).decode('ascii')})

    async def periodic(self):
        while True:
            await asyncio.sleep(self.gateway.interval)
            await self.recheck()

    async def receive_json(self, content, **kwargs):
        if type(content) is not dict:
            raise ValueError('terminal frame required')
        kind=content.get('type')
        if kind=='input' and set(content)=={'type','seq','data'}:
            if type(content['data']) is not str:
                raise ValueError('base64 input required')
            data=base64.b64decode(content['data'],validate=True)
            if not 1<=len(data)<=4096:
                raise ValueError('terminal input bound exceeded')
        elif kind=='resize' and set(content)=={'type','seq','cols','rows'}:
            size(content['cols'],content['rows'])
        else:
            raise ValueError('only terminal input/resize frames are accepted')
        await self.recheck()
        await self.gateway.runtime.database(self.gateway.terminals.before_input,self.actor,self.context,content['seq'])
        outcome='unknown'
        try:
            async with asyncio.timeout(2):
                if kind=='input':
                    if await self.stream.write(data)==len(data):
                        outcome='written'
                else:
                    await self.stream.resize(content['cols'],content['rows'])
                    outcome='written'
        except Exception:
            pass  # Receipt unknown; sequence уже сохранён, автоматического replay нет.
        await self.recheck()
        await self.send_json({'type':'ack','seq':content['seq'],'outcome':outcome})
        if outcome=='unknown':
            await self.finish(4409)

    async def cleanup(self):
        if getattr(self,'context',None) is None:
            return
        # Сначала запретить SQL допуск, затем дождаться physical retirement.
        # Ошибка/timeout detach оставляет unknown и эксклюзивный fence.
        await self.gateway.runtime.database(self.gateway.terminals.release,self.actor,self.context)
        proof=await self.gateway.streams.detach(self.context)
        await self.gateway.runtime.database(self.gateway.terminals.retire,self.context,proof)
        self.gateway.streams.forget(self.context,proof)
