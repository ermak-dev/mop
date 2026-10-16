"""Production ASGI/Channels root с явным lifespan и backpressure."""
import asyncio

from channels.routing import ProtocolTypeRouter
from django.core.asgi import get_asgi_application

from .runtime import Runtime, configure, validate_asgi_startup

configure()


async def unbound_websocket(scope, receive, send):
    """До #628 нет WS-авторизации/consumer; не создавать обход Auth."""
    await send({'type': 'websocket.close', 'code': 4404})


class ControllerApplication:
    """HTTP/gateway и adapters имеют одного владельца запуска и остановки."""
    def __init__(self, *, runtime=None, startup=validate_asgi_startup, http=None, http_limit=16):
        if not 1 <= http_limit <= 16:
            raise ValueError('HTTP concurrency must be within 16 requests')
        self.runtime = runtime or Runtime()
        self.startup = startup
        self.router = ProtocolTypeRouter({'http': http or get_asgi_application(),
                                          'websocket': unbound_websocket})
        self.http_limit = http_limit
        self.active = 0
        self.started = False

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            await self.lifespan(receive, send)
            return
        if scope['type'] == 'websocket':
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

    async def lifespan(self, receive, send):
        while True:
            message = await receive()
            if message['type'] == 'lifespan.startup':
                try:
                    await self.runtime.database(self.startup)
                    await self.runtime.start()
                    self.started = True
                except Exception as exc:
                    await self.runtime.aclose()
                    await send({'type': 'lifespan.startup.failed', 'message': str(exc)})
                    return
                await send({'type': 'lifespan.startup.complete'})
            elif message['type'] == 'lifespan.shutdown':
                self.started = False
                try:
                    async with asyncio.timeout(10):
                        while self.active:
                            await asyncio.sleep(.01)
                        await self.runtime.aclose()
                except Exception as exc:
                    await send({'type': 'lifespan.shutdown.failed', 'message': str(exc)})
                    return
                await send({'type': 'lifespan.shutdown.complete'})
                return


application = ControllerApplication()
