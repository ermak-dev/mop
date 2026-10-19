"""Rootless metadata/lease root плюс два конечных protected application ports."""
import asyncio
from dataclasses import dataclass, field
from threading import Event
import time
from contextvars import ContextVar

from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from mop.common import credentials as p
from mop.common.endpoint import TrustedEndpointContext
from mop.server.access.services import Denied, NotVisible
from mop.server.sessions.endpoints import Endpoints, Unavailable, Conflict, Capacity


_take_budget = ContextVar('private_take_budget', default=None)


class ProtectedEndpoints(Endpoints):
    def take(self, context):
        # Context-only gateway ещё не связан с исходным credential. Не claim-им
        # queued payload перед Unbound cipher: это потеряло бы попытку доставки.
        raise Unavailable('private delivery requires a current connection credential binding')

    @sensitive_variables()
    def _protected(self, method, credential, **args):
        if transaction.get_connection().in_atomic_block:
            raise RuntimeError('protected private delivery I/O must run outside SQL transaction')
        from .adminservices import credential_client
        client = credential_client()
        if client is None:
            raise Unavailable('private delivery credential service is not bound')
        budget = _take_budget.get()
        if budget is not None:
            deadline, stopped = budget
            remaining = deadline - time.monotonic()
            if stopped.is_set() or remaining <= 0:
                raise Unavailable('private delivery protected deadline expired')
            # Factory возвращает отдельный реальный client на каждый вызов;
            # очередь Runtime никогда не добавляет новый socket budget.
            client.timeout = min(client.timeout, remaining, 5)
        try:
            return getattr(client, method)(credential, **args)
        except p.CredentialInput:
            raise
        except p.PrivateDeliveryError as error:
            kind = {'conflict': Conflict, 'capacity': Capacity, 'not_found': NotVisible,
                    'invalid': ValueError}[error.code]
            raise kind(str(error)) from None
        except p.CredentialRefused as error:
            raise Denied(str(error)) from None
        except p.CredentialError as error:
            raise Unavailable(str(error)) from None

    @sensitive_variables()
    def send_private_report(self, credential, **message):
        return self._protected('send_private_report', credential, **message)

    @sensitive_variables()
    def take_private_delivery(self, credential, *, endpoint_id, connection_nonce):
        # Native/WS binding передаёт исходный credential отдельно от lease.
        # TrustedEndpointContext сам по себе не даёт protected plaintext port.
        return self._protected('take_private_delivery', credential,
            endpoint_id=endpoint_id, connection_nonce=connection_nonce)


@dataclass(eq=False, repr=False)
class ProtectedReceiver:
    credential: p.Credential | None = field(repr=False)
    context: TrustedEndpointContext = field(repr=False)
    closing: Event = field(default_factory=Event, repr=False)
    jobs: set = field(default_factory=set, repr=False)

    def close(self):
        self.closing.set()


class ProtectedReceivers:
    """Root-owned восемь admissions, без частной очереди/нового executor."""
    def __init__(self, runtime, endpoints):
        self.runtime, self.endpoints = runtime, endpoints
        self.receivers = {}
        self.jobs = set()
        self.stopped = False

    def bind(self, credential, context):
        if (self.stopped or type(credential) is not p.Credential
                or type(context) is not TrustedEndpointContext or context in self.receivers):
            raise Unavailable('private receiver credential binding unavailable')
        receiver = ProtectedReceiver(credential, context)
        self.receivers[context] = receiver
        return receiver

    async def take_delivery(self, context):
        from .runtime import Backpressure
        receiver = self.receivers.get(context)
        if self.stopped or receiver is None or receiver.closing.is_set():
            raise Unavailable('private receiver credential binding unavailable')
        if receiver.jobs or len(self.jobs) >= 8:
            raise Backpressure('private protected take capacity exhausted')
        deadline = time.monotonic() + 5
        stopped = Event()
        def invoke():
            if stopped.is_set() or receiver.closing.is_set() or time.monotonic() >= deadline:
                raise Unavailable('private protected take expired before submission')
            token = _take_budget.set((deadline, stopped))
            try:
                return self.endpoints.take_private_delivery(receiver.credential,
                    endpoint_id=context.endpoint_id, connection_nonce=context.nonce)
            finally:
                _take_budget.reset(token)
        job = asyncio.create_task(self.runtime.blocking(invoke))
        receiver.jobs.add(job)
        self.jobs.add(job)
        def completed(value):
            receiver.jobs.discard(value)
            self.jobs.discard(value)
            if not value.cancelled():
                value.exception()
        job.add_done_callback(completed)
        try:
            async with asyncio.timeout(max(0, deadline-time.monotonic())):
                result = await asyncio.shield(job)
            if receiver.closing.is_set() or stopped.is_set() or time.monotonic() >= deadline:
                raise Unavailable('private protected take completed after close or deadline')
            return result
        finally:
            # Не cancel-им job: Runtime продолжает учитывать настоящий worker,
            # а queued invoke проверит stop до первого socket request.
            stopped.set()

    async def close_receiver(self, context):
        receiver = self.receivers.get(context)
        if receiver is None:
            return
        receiver.close()
        if receiver.jobs:
            _, pending = await asyncio.wait(tuple(receiver.jobs), timeout=5)
            if pending:
                raise RuntimeError('private protected receiver shutdown incomplete')
        receiver.credential = None
        self.receivers.pop(context, None)

    async def aclose(self):
        self.stopped = True
        for receiver in self.receivers.values():
            receiver.close()
        await asyncio.gather(*(self.close_receiver(context) for context in tuple(self.receivers)))
