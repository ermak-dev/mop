"""Gateway use case: текущая authority, ORM claim, bounded HTTP, повторная проверка."""
import asyncio
import time
from urllib.parse import urlsplit

from django.db import DatabaseError
from mop.server.access.services import Denied
from mop.server.controller.runtime import Backpressure
from mop.server.secrets.services import Conflict, Refused, StorageUnavailable
from mop.server.management_policy import guard_management_write, redact_management_read, private_material, REDIRECT_MAX
from .authority import CredentialSource, HumanAuthority
from .breaker import Breaker
from .policy import MAX_BODY, UNSAFE, validate, request_headers, browser_headers
from .transport import TransportUnavailable
from .values import Reply, refusal


class Gateway:
    def __init__(self, *, runtime, source=None, transport=None, authority=None,
                 breaker=None, wait_seconds=1):
        if not 0 < wait_seconds <= 5:
            raise ValueError('invalid gateway wait budget')
        self.runtime = runtime
        self.source = source or CredentialSource()
        self.transport = transport
        self.authority = authority or HumanAuthority()
        self.breaker = breaker or Breaker()
        self.wait_seconds = wait_seconds

    def _authorize(self, actor, credential=None):
        self.authority.authorize(actor)
        if credential is not None and not self.source.current(actor, credential):
            raise Conflict('proxy management credential changed')

    def _claim(self, actor, credential, write):
        self._authorize(actor, credential)
        return self.breaker.claim(credential, write=write)

    async def request(self, actor, method, target, body, headers, *, panel=False):
        try:
            return await self.runtime.effect(self._request, actor, method, target, body, headers, panel=panel)
        except Denied as exc:
            return refusal(403, str(exc))
        except Backpressure:
            return refusal(429, 'proxy management capacity exhausted')
        except DatabaseError:
            return refusal(503, 'proxy management authority unavailable')
        except (Conflict, Refused, StorageUnavailable) as exc:
            # Безопасные причины public SecretStore не санитизируются повторно.
            return refusal(503, str(exc))
        except (PermissionError, ValueError):
            return refusal(503, 'proxy management credential unavailable')

    async def _request(self, actor, method, target, body, headers, *, panel, redirects=0):
        await self.runtime.database(self._authorize, actor)
        invalid = validate(method, target, body, panel=panel)
        if invalid:
            return refusal(invalid, 'proxy management request refused')
        if self.transport is None:
            return refusal(503, 'proxy management upstream is not bound')
        credential = await self.runtime.database(self.source.get, actor)
        if credential is None:
            return refusal(503, 'proxy management credential unavailable')
        await self.runtime.database(self._authorize, actor, credential)
        route = urlsplit(target).path
        if method in UNSAFE:
            guard = await self.runtime.blocking(guard_management_write, method, route, body, lambda: credential.value)
            if guard['action'] == 'refuse':
                return refusal(guard['status'], 'proxy management write refused')
            if guard['action'] == 'inject':
                body = guard['body']
            if len(body) > MAX_BODY:
                return refusal(413, 'proxy management request too large')
        until = time.monotonic() + self.wait_seconds
        while True:
            claim = await self.runtime.database(self._claim, actor, credential, method in UNSAFE)
            if claim.state != 'waiting':
                break
            if time.monotonic() >= until:
                # Ожидание caller ничего не меняет в физическом claim.
                await self.runtime.database(self._authorize, actor, credential)
                return refusal(429, 'proxy management request still running')
            await asyncio.sleep(.025)
        if claim.state == 'unknown':
            return refusal(409, 'proxy management outcome unknown; reconciliation required')
        if claim.state != 'claimed':
            return refusal(503, 'proxy management key rejected or cooldown active')
        status = None
        started = False
        try:
            await self.runtime.database(self._authorize, actor, credential)
            started = True
            try:
                reply = await self.runtime.blocking(self.transport.call, method, target, body,
                    request_headers(headers), credential)
                if type(reply) is not Reply or not 100 <= reply.status <= 599:
                    raise TransportUnavailable()
                status = reply.status
            except Exception as exc:
                # Сырые exception/URLs/provider body не выходят за adapter.
                if isinstance(exc, TransportUnavailable) and exc.receipt_status in (401, 403):
                    status = exc.receipt_status
                return refusal(504 if method in UNSAFE else 502,
                    'proxy management write outcome unknown' if method in UNSAFE else 'proxy management upstream unavailable')
        finally:
            # Runtime.blocking ждёт quiescence даже при отмене caller. При
            # отмене ДО старта можно освободить claim, ПОСЛЕ — только receipt.
            await self.runtime.database(self.breaker.complete, claim, status if started else 400)
        await self.runtime.database(self._authorize, actor, credential)
        if status in (401, 403):
            return refusal(503, 'proxy management key rejected')
        if 300 <= status < 400:
            location = dict(reply.headers).get('Location', '')
            if panel and location in ('/management.html', 'management.html') and redirects < REDIRECT_MAX - 1:
                return await self._request(actor, method, '/management.html', body, headers,
                    panel=True, redirects=redirects + 1)
            return refusal(502, 'proxy management redirect refused')
        if len(reply.body) > MAX_BODY or credential.value.encode() in reply.body:
            return refusal(502, 'proxy management unsafe response refused')
        # Запись может echo-нуть весь конфиг: redaction не только на GET.
        got = await self.runtime.blocking(redact_management_read, route, reply.body, credential.value)
        heads = browser_headers(reply.headers, panel=panel)
        if got is None or any(private_material(value, credential.value) for _, value in heads):
            return refusal(502, 'proxy management config unreadable')
        await self.runtime.database(self._authorize, actor, credential)
        return Reply(status, got, heads)
