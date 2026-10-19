"""Production ASGI/Channels root с явным lifespan и backpressure."""
import asyncio

from channels.routing import ProtocolTypeRouter
from django.core.asgi import get_asgi_application

from .runtime import Runtime, configure, validate_asgi_startup

configure()


class ControllerApplication:
    """HTTP/gateway и adapters имеют одного владельца запуска и остановки."""
    def __init__(self, *, runtime=None, startup=validate_asgi_startup, http=None, http_limit=16, gateway=None, realtime_gateway=None):
        if not 1 <= http_limit <= 16:
            raise ValueError('HTTP concurrency must be within 16 requests')
        self.runtime = runtime or Runtime()
        self.startup = startup
        from mop.server.realtime.gateway import Gateway
        from mop.server.gateway.composition import bind_http
        self.gateway = realtime_gateway or Gateway(runtime=self.runtime)
        self.router = ProtocolTypeRouter({'http': bind_http(http or get_asgi_application(), self.runtime, gateway),
                                          'websocket': self.gateway})
        self.http_limit = http_limit
        self.active = 0
        self.started = False
        self._cleanup = None

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            await self.lifespan(receive, send)
            return
        if scope['type'] == 'websocket':
            if not self.started:
                await send({'type':'websocket.close','code':1013})
                return
            await self.router(scope, receive, send)
            return
        if not self.started or self.active >= self.http_limit:
            await send({'type': 'http.response.start', 'status': 503,
                        'headers': [(b'cache-control', b'no-store'), (b'retry-after', b'1')]})
            await send({'type': 'http.response.body', 'body': b'controller unavailable'})
            return
        self.active += 1
        try:
            await self.router(scope, receive, send)
        finally:
            self.active -= 1

    async def _drain(self):
        async def http():
            while self.active:
                await asyncio.sleep(.01)
        # Оба drain остаются у root даже после внешнего deadline/cancel.
        results = await asyncio.gather(self.gateway.aclose(), http(), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
        await self.runtime.aclose()

    async def aclose(self):
        self.started = False
        stop = getattr(self.gateway, 'stop_accepting', None)
        if stop is not None:
            stop()
        if self._cleanup is None or (self._cleanup.done() and self._cleanup.exception() is not None):
            self._cleanup = asyncio.create_task(self._drain())
            self._cleanup.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        deadline = asyncio.get_running_loop().time() + 10
        cancelled = None
        while True:
            try:
                _, pending = await asyncio.wait((self._cleanup,), timeout=max(0, deadline-asyncio.get_running_loop().time()))
                break
            except asyncio.CancelledError as error:
                # Повторная отмена не теряет owner и не растягивает 10s budget.
                cancelled = error
        if pending:
            raise RuntimeError('controller shutdown incomplete: owned cleanup is still running')
        self._cleanup.result()
        if cancelled is not None:
            raise cancelled

    async def lifespan(self, receive, send):
        while True:
            message = await receive()
            if message['type'] == 'lifespan.startup':
                try:
                    await self.runtime.database(self.startup)
                    await self.runtime.start()
                    await self.gateway.start()
                    self.started = True
                except BaseException as exc:
                    # Частичный gateway сохраняет cleanup SQL до закрытия Runtime.
                    try:
                        await self.aclose()
                    except Exception as cleanup:
                        exc = cleanup
                    if isinstance(exc, asyncio.CancelledError):
                        raise exc
                    await send({'type': 'lifespan.startup.failed', 'message': str(exc)})
                    return
                await send({'type': 'lifespan.startup.complete'})
            elif message['type'] == 'lifespan.shutdown':
                try:
                    await self.aclose()
                except Exception as exc:
                    await send({'type': 'lifespan.shutdown.failed', 'message': str(exc)})
                    return
                await send({'type': 'lifespan.shutdown.complete'})
                return


application = ControllerApplication()
