"""Root удерживает actual native attachment и finite proxy в своём процессе."""
import asyncio
from dataclasses import dataclass, field

from mop.driver.launch import Registration
from mop.node.host_inputs import Envelope, MAX_FILE_BYTES, _json
from mop.node.host_native import ModelInputs, NativeSubmission
from mop.node.host_native_lifetime import NativeMailbox
from mop.node.host_phases import Projection, cutoff
from mop.node.host_root import _read
from mop.node.host_scope import Refused, Unknown, canonical, decode, encode, exact
from mop.node.host_scope_supervisor import read_frame
from mop.node.http_execution import Unavailable


@dataclass(repr=False)
class NativeOwner:
    owner: object
    body: object
    item: object
    mailbox: NativeMailbox
    task: object = None
    complete: asyncio.Event = field(default_factory=asyncio.Event)
    error: str | None = None
    tasks: set = field(default_factory=set)
    identifiers: set = field(default_factory=set)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    physical: object = None
    launched: object = None
    cleanup: object = None

    async def check(self):
        await self.owner._peer(self.body)
        if self.physical is not None:
            current = await self.owner.adapter.native_attachment(self.body.reference,
                self.mailbox.producer_report, self.mailbox.attachment,
                self.owner.manifest.tools, self.item.submission.pins)
            if current != self.physical:
                raise Refused('retained actual native attachment changed')
        elif self.launched is not None:
            native, tmux = (self.launched[name]['process'] for name in ('native', 'tmux'))
            value = dict(pid=native['pid'], process_start=native['boot_id']+':'+str(native['start']),
                session_id=self.launched['session_id'], tmux_pid=tmux['pid'], tmux_start=tmux['start'])
            if await self.owner.adapter.native_launch(self.body.reference, value,
                    self.owner.manifest.tools, self.item.submission.pins) != self.launched:
                raise Refused('retained original launched native changed before producer ACK')

    def status(self):
        physical = self.physical or self.launched
        return dict(producer=self.mailbox.producer_ack.wire() if self.mailbox.producer_ack else None,
            attachment=({key: physical[key] for key in ('reference', 'runner', 'session_id')} | {
                name: {key: physical[name][key] for key in ('process', 'executable')}
                for name in ('native', 'tmux')}) if physical else None,
            committed=self.mailbox.model_committed.wire() if self.mailbox.model_committed else None,
            pending=self.mailbox.pending, error=self.error)

    async def reply(self, identifier, *, answer=None, error=None):
        async with self.write_lock:
            writer = self.body.streams[1]
            writer.write(encode(dict(schema_version=1, action='native_reply', id=identifier,
                answer=answer, error=error))+b'\n')
            await writer.drain()

    async def request(self, value):
        identifier = value['id']
        stage = 'peer'
        try:
            await self.check()
            operation, payload = value['operation'], value['payload']
            stage = operation if operation in ('checkpoint', 'native_launched', 'register_producer', 'ingest_telemetry') else 'operation'
            if operation == 'checkpoint':
                exact(payload, ('effect', 'sequence'))
                await self.owner.checkpoint(self.item, payload['effect'], payload['sequence'])
                await self.check()
                answer = dict(approved=payload['effect'], sequence=payload['sequence'])
            elif operation == 'native_launched':
                exact(payload, ('pid', 'process_start', 'session_id', 'tmux_pid', 'tmux_start'))
                if self.item.last_effect != 'native-launch' or self.launched is not None:
                    raise Refused('native launch report cannot replay or replace original processes')
                self.launched = await self.owner.adapter.native_launch(self.body.reference, payload,
                    self.owner.manifest.tools, self.item.submission.pins)
                answer = dict(retained=True)
            else:
                if operation == 'register_producer':
                    if self.item.last_effect not in ('producer-attach', 'desired-switch'):
                        raise Refused('producer registration before fresh native attachment checkpoint')
                    self.mailbox.validate(operation, payload)
                    physical = await self.owner.adapter.native_attachment(self.body.reference,
                        self.mailbox.producer_report, self.mailbox.attachment,
                        self.owner.manifest.tools, self.item.submission.pins)
                    if self.launched is None or any(physical[key] != self.launched[key] for key in self.launched):
                        raise Refused('native producer differs from original retained launched processes')
                    if self.physical is not None and self.physical != physical:
                        raise Refused('native lifetime cannot adopt replacement physical process')
                    self.physical = physical
                answer = await self.mailbox.request(operation, payload)
                await self.check()
            await self.reply(identifier, answer=answer)
        except (Refused, Unknown, Unavailable, OSError, TimeoutError, ValueError) as error:
            reason = str(error) if isinstance(error, (Refused, Unknown, Unavailable)) else 'native root '+stage+' failed: '+type(error).__name__
            await self.reply(identifier, error=reason)
        finally:
            self.identifiers.discard(identifier)

    def _done(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.error = 'native control request failed: '+type(task.exception()).__name__
            self.complete.set()
            self.owner._phase_cleanup_task(self.body)

    async def run(self):
        reader, _, _ = self.body.streams
        try:
            while True:
                value = decode(await read_frame(reader))
                # Failure не выдаёт authority: сохраняем безопасную причину из
                # original pinned stream даже если MainPID уже завершился.
                if type(value) is dict and value.get('action') == 'body_failed':
                    exact(value, ('schema_version', 'action', 'attempt_id', 'error'))
                    if (type(value['schema_version']) is not int or value['schema_version'] != 1
                            or value['attempt_id'] != self.item.submission.admission.attempt_id):
                        raise Refused('foreign native failure attempt')
                    reason = value['error']
                    if type(reason) is not str or not 0 < len(reason) <= 1024 or not reason.isprintable():
                        raise Refused('bounded native failure reason required')
                    raise Unknown(reason)
                await self.check()
                if type(value) is dict and value.get('action') == 'native_complete':
                    exact(value, ('schema_version', 'action', 'attempt_id', 'committed_sequence'))
                    row = self.mailbox.model_committed
                    if (type(value['schema_version']) is not int or value['schema_version'] != 1
                            or value['attempt_id'] != self.item.submission.admission.attempt_id
                            or row is None or value['committed_sequence'] != str(row.sequence)
                            or self.item.last_effect != 'desired-switch' or self.complete.is_set()):
                        raise Refused('native completion requires original producer ACK and genuine committed desired switch')
                    self.complete.set()
                    continue
                exact(value, ('schema_version', 'action', 'id', 'operation', 'payload'))
                if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['action'] != 'native_request':
                    raise Refused('finite native body request required')
                identifier = canonical(value['id'])
                if identifier in self.identifiers or len(self.tasks) >= 4:
                    raise Refused('native body control capacity or replay refused')
                self.identifiers.add(identifier)
                task = asyncio.create_task(self.request(value))
                self.tasks.add(task)
                task.add_done_callback(self._done)
        except (Refused, Unknown, OSError, TimeoutError, ValueError) as error:
            self.error = str(error) if isinstance(error, (Refused, Unknown)) else type(error).__name__
            self.complete.set()
            self.owner._phase_cleanup_task(self.body)
        except asyncio.CancelledError:
            raise

    async def _close(self):
        self.mailbox.close()
        if self.task is not None:
            self.task.cancel()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*(tuple(self.tasks)+(self.task,) if self.task else tuple(self.tasks)), return_exceptions=True)

    async def aclose(self):
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self._close())
        await asyncio.shield(self.cleanup)


def submit_native(owner, request):
    from mop.node.host_native_body import installed_profile
    from mop.node.host_phase_supervisor import Attempt
    if type(request) is not NativeSubmission or owner._closed or not owner._confirmed:
        raise Refused('native requires the current bound confirmed root HOST owner')
    scope, a = request.reference.scope, request.admission
    if (scope.epoch, scope.node_id, scope.executor_epoch) != (owner._bound_epoch, owner.identity.node_id, owner._backend_epoch):
        raise Refused('native root identity/epoch changed')
    if (scope, a.attempt_id) in owner.attempts or len(owner.attempts) >= owner.capacity:
        raise Refused('native replay or retained attempt capacity refused')
    body = owner.bodies.get(scope)
    if (body is None or body.reference != request.reference or owner._owned.get(scope) != request.reference
            or body.native is not None or body.projection != request.projection
            or body.completed != ['prepare_body', 'bootstrap_body', 'initial_identity']
            or any(not item.acknowledged for item in body.attempts) or body.cleanup is not None):
        raise Refused('native requires original body and three exact committed phase ACKs')
    from mop.node.host_native import status_budget
    status_budget(request, owner.manifest)
    owner.manifest.require_native_release(owner.installed.runner.parents[2])
    # Role canonicalizer использует declared PyYAML. Только заранее hashed
    # installed dependency tree; -S остаётся, .pth/user-site не исполняются.
    owner.manifest.verify()
    import sys
    python_root = str(owner.manifest.directories['python_root'])
    if python_root not in sys.path:
        sys.path.insert(0, python_root)
    profile = installed_profile(owner.installed.runner.parents[2])
    if profile.ca != owner.manifest.tools['ca']:
        raise Refused('native provider CA differs from immutable installed runtime')
    _, deadline = cutoff(a)
    store = owner._store()
    receipt = store.snapshot(owner.installed.node_uid, request.descriptor, a)
    try:
        envelope = Envelope.parse(_read(owner.config.snapshots/request.descriptor.filename, limit=MAX_FILE_BYTES),
            request.descriptor, a, backend_epoch=owner._bound_epoch, node_id=owner.identity.node_id,
            executor_epoch=owner._backend_epoch)
        value = _json(envelope.payload)
        if Projection.from_delivery(value) != request.projection or ModelInputs.parse(value, profile).pins() != request.pins:
            raise Refused('native root snapshot differs from admitted model/projection pins')
    except BaseException:
        store.cleanup_input(receipt)
        store.cleanup_snapshot(receipt)
        store.forget(receipt)
        raise
    item = Attempt(request, receipt, deadline)
    registration = Registration(a.work, request.projection.bootstrap_revision,
        request.projection.bootstrap_digest, request.projection.image_digest, request.projection.display_name)
    body.native = NativeOwner(owner, body, item, NativeMailbox(registration, a, desired_model=request.pins.desired_model))
    owner.attempts[scope, a.attempt_id] = item
    body.attempts.append(item)
    item.task = asyncio.create_task(run_native(owner, body, item))
    return item


async def run_native(owner, body, item):
    try:
        async with asyncio.timeout_at(item.deadline):
            await owner._peer(body, singleton=True)
            writer = body.streams[1]
            writer.write(item.submission.encode()+b'\n')
            await writer.drain()
            life = body.native
            life.task = asyncio.create_task(life.run())
            await life.complete.wait()
            if life.error is not None:
                raise Unknown(life.error)
            await life.check()
            if life.mailbox.model_committed is None:
                raise Refused('native desired model telemetry COMMIT is absent')
            owner._clean_input(item)
            item.status = 'succeeded'
    except asyncio.CancelledError:
        item.status, item.error = 'unknown', 'native phase cancelled; original physical cleanup retained'
        await owner._cleanup_phase(body)
        raise
    except (Refused, Unknown, OSError, TimeoutError, ValueError) as error:
        item.status = 'unknown'
        item.error = str(error) if isinstance(error, (Refused, Unknown)) else (
            'native original effect cutoff expired' if isinstance(error, TimeoutError) else type(error).__name__)
        await owner._cleanup_phase(body)


async def mailbox_exchange(owner, request, worker):
    """Один короткий poll/complete; node peer уже pinned SocketServer'ом."""
    from mop.node.host_scope import Scope
    action = request.get('action')
    keys = ('schema_version', 'action', 'scope', 'attempt_id')
    exact(request, keys if action == 'native_poll' else (*keys, 'id', 'ticket', 'answer', 'error'))
    if type(request['schema_version']) is not int or request['schema_version'] != 1:
        raise Refused('unsupported native mailbox exchange')
    scope, attempt = Scope.parse(request['scope']), canonical(request['attempt_id'])
    item = owner._attempt(scope, attempt)
    body = owner.bodies[scope]
    life = body.native
    if life is None or life.item is not item or body.cleanup is not None:
        raise Refused('native mailbox has no original live retained attachment')
    await life.check()
    if action == 'native_poll':
        return dict(operation=life.mailbox.poll(worker))
    if action != 'native_complete':
        raise Refused('native mailbox exchange action is not enabled')
    if request['error'] is not None:
        life.mailbox.fail(worker, request['id'], request['ticket'], request['error'])
    else:
        life.mailbox.complete(worker, request['id'], request['ticket'], request['answer'])
    await life.check()
    return dict(completed=request['id'])
