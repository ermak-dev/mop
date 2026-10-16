"""ASGI ingress bound к runtime владельца; default source/transport закрыты."""
import asyncio
from .policy import MAX_BODY
from .service import Gateway


def management_path(path):
    return path in ('/llm/management.html', '/llm/raw/management.html') or path.startswith('/v8/management/')


class GatewayHTTP:
    def __init__(self, http, gateway):
        self.http, self.gateway = http, gateway

    async def __call__(self, scope, receive, send):
        if not management_path(scope.get('path', '')):
            return await self.http(scope, receive, send)
        lengths = [value for name, value in scope.get('headers', ()) if name.lower() == b'content-length']
        if len(lengths) > 1 or lengths and (not lengths[0].isdigit() or len(lengths[0]) > 16):
            return await self._refuse(send, 400)
        declared = int(lengths[0]) if lengths else None
        if declared is not None and declared > MAX_BODY:
            return await self._refuse(send, 413)
        scope = {**scope, 'mop.gateway': self.gateway}
        size, chunks = 0, []
        try:
            async with asyncio.timeout(5):
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    chunk = message.get('body', b'')
                    size += len(chunk)
                    if size > MAX_BODY:
                        return await self._refuse(send, 413)
                    chunks.append(chunk)
                    if not message.get('more_body', False):
                        break
        except TimeoutError:
            return await self._refuse(send, 408)
        if declared is not None and size != declared:
            return await self._refuse(send, 400)
        body = b''.join(chunks)
        scope['mop.gateway.body'] = body
        consumed = False
        async def bounded_receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            return await receive()
        return await self.http(scope, bounded_receive, send)

    @staticmethod
    async def _refuse(send, status):
        await send({'type': 'http.response.start', 'status': status,
            'headers': [(b'cache-control', b'no-store'), (b'content-type', b'text/plain')]})
        await send({'type': 'http.response.body', 'body': b'proxy management body refused'})


def bind_http(http, runtime, gateway=None):
    if gateway is not None and gateway.runtime is not runtime:
        raise ValueError('proxy management gateway runtime must match controller owner')
    return GatewayHTTP(http, gateway if gateway is not None else Gateway(runtime=runtime))
