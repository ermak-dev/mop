"""Django session/CSRF boundary; external HTTP обслуживает owned async gateway."""
from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponse
from django.middleware.csrf import get_token
from django.db import DatabaseError
from mop.server.controller.runtime import Backpressure
from .authority import HumanAuthority
from .panel import shim, adapt, CSP
from .policy import MAX_BODY, UNSAFE
from .values import refusal


def response(reply):
    result = HttpResponse(reply.body, status=reply.status)
    for name, value in reply.headers:
        result[name] = value
    result['Cache-Control'] = 'no-store'
    result['X-Content-Type-Options'] = 'nosniff'
    result['Referrer-Policy'] = 'no-referrer'
    result['Content-Security-Policy'] = CSP
    return result


async def gateway_view(request, resource=None):
    try:
        return await _gateway_view(request)
    except Backpressure:
        return response(refusal(429, 'proxy management capacity exhausted'))
    except DatabaseError:
        return response(refusal(503, 'proxy management authority unavailable'))


async def _gateway_view(request):
    path = request.path
    is_panel = path in ('/llm/management.html', '/llm/raw/management.html')
    gateway = getattr(request, 'scope', {}).get('mop.gateway')
    if gateway is None:
        return response(refusal(503, 'proxy management gateway is not bound'))
    try:
        # Никаких token fallback/request.user flags. Session сверяется с БД
        # только через Runtime, которым владеет ASGI composition.
        actor = await gateway.runtime.database(HumanAuthority().session, request.session.session_key)
    except (PermissionError, ValueError):
        return response(refusal(403, 'proxy management permission required'))
    if is_panel and request.method != 'GET':
        return response(refusal(405, 'proxy management panel is read only'))
    if request.method in UNSAFE and request.headers.get('Origin') != request.scheme + '://' + request.get_host():
        return response(refusal(403, 'proxy management origin refused'))
    if path == '/llm/management.html':
        from .values import Reply
        get_token(request)
        return response(Reply(200, shim(), (('Content-Type', 'text/html; charset=utf-8'),)))
    try:
        length = request.headers.get('Content-Length', '0')
        if not length.isdecimal():
            return response(refusal(400, 'proxy management content length refused'))
        if int(length) > MAX_BODY:
            return response(refusal(413, 'proxy management request too large'))
        # Ingress уже сохранил bounded bytes до Django multipart/CSRF parser.
        # Повторное request.body после multipart теряет исходный upload;
        # чтение spooled body на ASGI loop также недопустимо.
        body = getattr(request, 'scope', {}).get('mop.gateway.body')
        if body is None:
            body = await gateway.runtime.blocking(lambda: request.body)
    except RequestDataTooBig:
        return response(refusal(413, 'proxy management request too large'))
    target = '/management.html' if is_panel else request.get_full_path()
    reply = await gateway.request(actor, request.method, target, body, dict(request.headers), panel=is_panel)
    if is_panel and reply.status == 200:
        try:
            html = await gateway.runtime.blocking(adapt, reply.body, get_token(request))
        except (ValueError, UnicodeError):
            return response(refusal(502, 'proxy management panel unreadable'))
        # Даже rewrite/CSRF wait требует текущую session authority.
        try:
            await gateway.runtime.database(HumanAuthority().session, request.session.session_key)
        except (PermissionError, ValueError):
            return response(refusal(403, 'proxy management permission required'))
        from .values import Reply
        reply = Reply(200, html, (('Content-Type', 'text/html; charset=utf-8'),))
    return response(reply)
