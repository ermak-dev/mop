"""Root-only вид следующей фазы над одним attached native producer (#643).

Registration.matches остаётся exact Work. HTTPS admission и addressed launch
доказываются заново; epoch/ACK/bootstrap принадлежат lifetime, не этому виду.
Ни poll, ни JSON caller, ни observer сами вида не создают. Root обязан передать
настоящий MachineClient и admission обычного Machine claim; EffectGuard не заменён.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from mop.driver.launch import RegisteredLayout, Registration
from mop.node.body_launch import BindingsResolver
from mop.node.execution_adapters import ActionInput
from mop.node.http_execution import Admission, HttpsTransport, MachineClient, Refused, Unavailable, Work
from mop.node.native_observer import NativePhysicalSessions


@dataclass(frozen=True)
class _CurrentLaunchApproval:
    # Не LaunchResponse: readiness не читает и не кеширует role/consumer bytes.
    registration: Registration
    inputs: ActionInput


@dataclass(frozen=True)
class CurrentLaunchAuthority:
    """Конечная capability root: только существующие authenticated machine API.

    Admission DTO сам по себе не доказательство: server recheck обязателен до и
    после выдачи bytes. Никаких callbacks, принимающих caller-selected JSON за
    authentication, новых claims, producer registrations или readiness receipts.
    """
    client: MachineClient
    registration: Registration
    admission: Admission

    def __post_init__(self):
        self.check()

    def check(self):
        r, a, c = self.registration, self.admission, self.client
        if (type(c) is not MachineClient or type(c.transport) is not HttpsTransport
                or type(r) is not Registration or type(r.work) is not Work
                or type(a) is not Admission or a.work != r.work
                or r.work.action not in ('apply_identity', 'confirm_ready')):
            raise Refused('exact authenticated current launch admission required')
        w = r.work
        if w.action == 'apply_identity':
            # Старый post-launch identity port не имеет отдельного purpose proof.
            # Initial identity до native exec -- другой action/physical contract.
            raise Unavailable('post-launch identity current view purpose is not bound')
        if Work.parse(w.wire()) != w:
            raise Refused('canonical current launch work required')
        w.gate(node_id=w.node_id, epoch=w.epoch, executor_epoch=w.executor_epoch)
        if not datetime.now(timezone.utc) < a.lease_until <= w.not_after:
            raise Refused('current launch admission lease expired or changed')

    async def fetch(self, current):
        # current проверяет physical lifetime перед/после каждого API await,
        # включая окна смены process/session во время admission recheck.
        self.check()
        client, transport = self.client, self.client.transport
        async def gate():
            self.check()
            if self.client.transport is not transport:
                raise Refused('authenticated current launch transport changed')
            await current()
            self.check()
            if self.client.transport is not transport:
                raise Refused('authenticated current launch transport changed')
        async with asyncio.timeout(5):
            await gate()
            await client.recheck(self.admission)
            await gate()
            value = await client.read_initial_launch(self.registration.work.wire())
            await gate()
            if any(value[name] != getattr(self.registration, name) for name in
                    ('bootstrap_revision', 'bootstrap_digest', 'image_digest', 'display_name')):
                raise Refused('current launch approval changed registration pins')
            inputs = ActionInput(value['git_name'], value['git_email'])
            inputs.check(self.registration.work.action)
            response = _CurrentLaunchApproval(self.registration, inputs)
            await client.recheck(self.admission)
            await gate()
        return response


def _same_lifetime(anchor, current):
    # Локальная политика конкретной root capability, НЕ общее subset-сравнение
    # Registration/Work. Только step/action/revision/deadline могут отличаться.
    for name in ('epoch', 'operation_id', 'target_id', 'incarnation_id', 'node_id',
                 'executor_epoch', 'owner_id', 'generation', 'role', 'bundle_digest', 'grant_id'):
        if getattr(anchor.work, name) != getattr(current.work, name):
            raise Refused('current view changed immutable producer binding: '+name)
    for name in ('bootstrap_revision', 'bootstrap_digest', 'image_digest', 'display_name'):
        if getattr(anchor, name) != getattr(current, name):
            raise Refused('current view changed immutable launch pin: '+name)


class _ViewPhysical(NativePhysicalSessions):
    async def observe(self, layout):
        self.owner._check()
        proof = await super().observe(layout)
        self.owner._check()
        return proof


class _CurrentProducerView(BindingsResolver):
    def __init__(self, runtime, authority):
        self.runtime, self.authority = runtime, authority
        self._anchor = runtime._layout
        self._proof, self._identity = runtime._proof, runtime._identity
        layout = RegisteredLayout(self._anchor.home, authority.registration)
        if (layout.clone, layout.runtime, layout.tmux_socket, layout.launcher) != (
                self._anchor.clone, self._anchor.runtime, self._anchor.tmux_socket, self._anchor.launcher):
            raise Refused('current producer view changed physical paths')
        self._approved_layout = layout
        self._failure, self._approved = None, False
        super().__init__(layout, source=self, physical=_ViewPhysical(owner=self))

    def _check(self):
        if self._failure is not None:
            raise self._failure
        if (self.runtime._state != 'active'
                or self.runtime._layout != self._anchor or self.runtime._proof != self._proof
                or self.runtime._identity != self._identity):
            raise Unavailable('current producer view is closed or physical lifetime changed')
        if self.layout != self._approved_layout or self.authority.registration != self.layout.registration:
            raise Refused('current producer view registration or paths changed')
        self.authority.check()

    async def _current(self):
        self._check()
        await self.runtime._current()
        self._check()

    async def fetch(self, work):
        self._check()
        if not self.layout.registration.matches(work):
            raise Refused('foreign current producer view work')
        try:
            response = await self.authority.fetch(self._current)
            self._check()
            return response
        except (Refused, Unavailable, TimeoutError) as error:
            self._failure = error
            raise

    async def resolve(self, work):
        approval = await self.fetch(work)
        if (type(approval) is not _CurrentLaunchApproval
                or approval.registration != self.layout.registration):
            raise Refused('typed current launch approval required')
        return await self._target(work, approval.inputs)

    async def proof(self, work):
        self._check()
        if not self._approved:
            raise Refused('current producer view has no authenticated approval')
        proof = await super().proof(work)
        self._check()
        return proof

    def producer_changed(self, layout):
        if self._approved and layout == self._approved_layout:
            self.runtime.producer_changed(self._anchor)

    async def producer_epoch(self, layout, identity):
        if not self._approved or layout != self._approved_layout:
            raise Refused('unapproved current producer view layout')
        await self.fetch(self.layout.registration.work)
        self._check()
        # Только lookup уже существующего epoch на исходном layout. Смена native
        # identity закрывает lifetime; новый view не получает нового ACK/epoch.
        epoch = await self.runtime.producer_epoch(self._anchor, identity)
        self._check()
        return epoch


async def current_view(runtime, authority):
    if runtime._state != 'active' or runtime._proof is None:
        raise Unavailable('current producer view requires active registration acknowledgement')
    if type(authority) is not CurrentLaunchAuthority:
        raise Refused('explicit authenticated current launch authority required')
    authority.check()
    _same_lifetime(runtime._layout.registration, authority.registration)
    view = _CurrentProducerView(runtime, authority)
    await view.fetch(authority.registration.work)
    view._check()
    view._approved = True
    return view
