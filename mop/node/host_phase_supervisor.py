"""Root ownership трёх planned phases; node bearer и project YAML здесь нет."""
import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
import stat
import time
from uuid import uuid4

from mop.common.initialcreate import CONFIRMATIONS
from mop.node.host_backend import Process
from mop.node.host_input_files import SnapshotStore
from mop.node.host_inputs import Envelope, MAX_FILE_BYTES, _json, admission_wire
from mop.node.host_phase_manifest import RuntimeManifest, path
from mop.node.host_phase_source import SourceCredential
from mop.node.host_phases import CHECKPOINTS, Checkpoint, Projection, Submission, address, cutoff, require_frame
from mop.node.host_root import _read
from mop.node.host_scope import Installed, Request, Scope, Refused, Unknown, decode, encode, exact
from mop.node.host_scope_supervisor import Supervisor, peer_credentials, read_frame
from mop.node.host_scope_systemd import open_directory


@dataclass(frozen=True)
class PhaseConfig:
    installed: Installed
    runtime_manifest: Path
    ingress: Path
    snapshots: Path
    control_root: Path

    @classmethod
    def load(cls, filename):
        value = _json(_read(filename, limit=8192))
        exact(value, ('schema_version', 'installed', 'runtime_manifest', 'ingress', 'snapshots', 'control_root'))
        if type(value['schema_version']) is not int or value['schema_version'] != 1:
            raise Refused('unsupported installed HOST phase configuration')
        v = value['installed']
        exact(v, ('node_uid', 'body_uid', 'body_gid', 'runner', 'body_root', 'node_private'))
        installed = Installed(v['node_uid'], v['body_uid'], v['body_gid'],
            *(path(v[k]) for k in ('runner', 'body_root', 'node_private')))
        result = cls(installed, *(path(value[k]) for k in ('runtime_manifest', 'ingress', 'snapshots', 'control_root')))
        if (result.ingress.parent != installed.node_private or result.snapshots == result.ingress
                or len(os.fsencode(result.control_root)) > 50):
            raise Refused('separate bounded installed HOST phase paths required')
        return result

    def control(self, scope):
        return self.control_root/(scope.launch_id+'.sock')


@dataclass
class Attempt:
    submission: Submission
    receipt: object
    deadline: float
    phase_receipt: str = field(default_factory=lambda: str(uuid4()))
    task: object = None
    pending: object = None
    last_effect: str | None = None
    approved: asyncio.Event = field(default_factory=asyncio.Event)
    status: str = 'accepted'
    error: str | None = None
    acknowledged: bool = False


@dataclass
class Body:
    scope: Scope
    projection: Projection
    workspace: tuple | None = None
    reference: object = None
    process: object = None
    listener: object = None
    socket_identity: tuple | None = None
    streams: object = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    completed: list = field(default_factory=list)
    attempts: list = field(default_factory=list)
    cleanup: object = None
    connection_error: str | None = None
    cleanup_connection: object = None
    native: object = None


class PhaseOwner(Supervisor):
    """Retained tasks/receipts не передаются frontend по одному status ACK."""
    def __init__(self, installed, adapter, *, identity, phase_config):
        super().__init__(installed, adapter, identity=identity)
        self.config = PhaseConfig.load(phase_config)
        if self.config.installed != installed:
            raise Refused('phase installation differs from physical backend')
        if installed.runner != Path(__file__).with_name('host_body.py'):
            raise Refused('planned HOST requires the one installed lifecycle broker')
        self.manifest = RuntimeManifest.load(self.config.runtime_manifest)
        self.manifest.require_release(Path(__file__).absolute().parents[2])
        self.bodies, self.attempts = {}, {}
        self.store = None
        self._body_handlers = set()
        fd = open_directory(self.config.control_root, owner=0, immutable=True)
        os.close(fd)

    def _store(self):
        if self.store is None:
            self.store = SnapshotStore(self.config.ingress, self.config.snapshots,
                node_uid=self.installed.node_uid, body_uid=self.installed.body_uid, body_gid=self.installed.body_gid,
                backend_epoch=self._bound_epoch, node_id=self.identity.node_id, executor_epoch=self._backend_epoch)
        return self.store

    def _attempt(self, scope, attempt):
        item = self.attempts.get((scope, attempt))
        if item is None:
            raise Refused('planned status has no retained original attempt')
        return item

    def status(self, item):
        body = self.bodies[item.submission.descriptor.scope]
        result = dict(status=item.status, receipt=item.phase_receipt, error=item.error,
            admission=admission_wire(item.submission.admission),
            reference=body.reference.wire() if body.reference else None,
            checkpoint=dict(effect=item.pending.effect, sequence=item.pending.sequence) if item.pending and not item.pending.approved else None,
            runtime_digest=self.manifest.content_digest, resources=self.manifest.project(body.projection).wire(),
            acknowledged=item.acknowledged)
        if getattr(body, 'native', None) is not None and body.native.item is item:
            result['native'] = body.native.status()
        return result

    def submit_native(self, request):
        from mop.node.host_native_owner import submit_native
        return submit_native(self, request)

    def submit(self, request):
        if self._closed or not self._confirmed:
            raise Refused('planned HOST backend is not bound and confirmed')
        scope, a = request.descriptor.scope, request.admission
        if (scope.epoch, scope.node_id, scope.executor_epoch) != (self._bound_epoch, self.identity.node_id, self._backend_epoch):
            raise Refused('planned HOST backend identity/epoch mismatch')
        if (scope, a.attempt_id) in self.attempts:
            raise Refused('planned submission replay forbidden; observe original attempt status')
        if len(self.attempts) >= self.capacity:
            raise Refused('planned retained attempt capacity exhausted')
        self.manifest.project(request.projection)
        body = self.bodies.get(scope)
        if a.work.action == 'prepare_body':
            if body is not None or scope in self._fences:
                raise Refused('planned workspace/start replay forbidden')
        elif (body is None or body.reference != request.reference or self._owned.get(scope) != request.reference
                or body.projection != request.projection or body.completed != list(CHECKPOINTS)[:len(body.completed)]
                or len(body.completed) != list(CHECKPOINTS).index(a.work.action)
                or any(not previous.acknowledged for previous in body.attempts)):
            raise Refused('planned phase requires prior exact committed phase ACK and retained reference')
        _, deadline = cutoff(a)
        store = self._store()
        receipt = store.snapshot(self.installed.node_uid, request.descriptor, a)
        # Только opaque canonical JSON и finite source format: root не разбирает YAML.
        try:
            envelope = Envelope.parse(_read(self.config.snapshots/request.descriptor.filename, limit=MAX_FILE_BYTES),
                request.descriptor, a, backend_epoch=self._bound_epoch, node_id=self.identity.node_id, executor_epoch=self._backend_epoch)
            value = _json(envelope.payload)
            if Projection.from_delivery(value) != request.projection:
                raise Refused('root snapshot projection differs from selected metadata')
            if a.work.action == 'prepare_body':
                import base64
                consumers = value['consumers']
                if len(consumers) != 1 or consumers[0]['purpose'] != 'source_credential':
                    raise Refused('exact supported source credential required before workspace effect')
                SourceCredential.parse(_json(base64.b64decode(consumers[0]['value'], validate=True)), request.projection.source['origin'])
        except BaseException:
            store.cleanup_input(receipt)
            store.cleanup_snapshot(receipt)
            store.forget(receipt)
            raise
        if body is None:
            body = Body(scope, Projection.parse(request.projection.wire()))
            self.bodies[scope] = body
        item = Attempt(request, receipt, deadline)
        self.attempts[scope, a.attempt_id] = item
        body.attempts.append(item)
        item.task = asyncio.create_task(self._run(body, item))
        return item

    async def checkpoint(self, item, effect, sequence):
        if time.monotonic() >= item.deadline or item.pending is not None:
            raise Refused('planned original effect cutoff expired or checkpoint overlaps')
        from mop.node.host_phases import checkpoint_plan
        plan = checkpoint_plan(item.submission.admission.work.action)
        if type(sequence) is not int or not 0 <= sequence < len(plan) or plan[sequence] != effect:
            raise Refused('planned compiled effect sequence changed')
        item.pending, item.approved = Checkpoint(effect, sequence), asyncio.Event()
        await item.approved.wait()
        if time.monotonic() >= item.deadline or not item.pending.approved:
            raise Refused('planned effect approval expired')
        item.last_effect = effect
        item.pending = None

    async def _listener(self, body):
        pathname = self.config.control(body.scope)
        if os.path.lexists(pathname):
            raise Refused('planned control socket collision; no adoption')
        def connected(reader, writer):
            task = asyncio.create_task(self._body_connection(body, reader, writer))
            self._body_handlers.add(task)
            task.add_done_callback(self._body_handlers.discard)
        body.listener = await asyncio.start_unix_server(connected, path=str(pathname), limit=4097, backlog=1)
        os.chown(pathname, 0, self.installed.body_gid)
        os.chmod(pathname, 0o660)
        info = pathname.lstat()
        body.socket_identity = info.st_dev, info.st_ino

    async def _body_connection(self, body, reader, writer):
        retained = False
        try:
            peer = peer_credentials(writer.get_extra_info('socket'))
            if peer[1:] != (self.installed.body_uid, self.installed.body_gid):
                raise Refused('planned control caller is not the installed body UID/GID')
            async with asyncio.timeout(10.):
                hello = decode(await read_frame(reader))
                if type(hello) is dict and hello.get('action') == 'cleanup_hello':
                    require_frame(hello, dict(schema_version=1, action='cleanup_hello', scope=body.scope.wire()))
                    connection = body.cleanup_connection
                    if body.cleanup is None or connection is None or connection.done():
                        raise Refused('cleanup control has no retained original cleanup owner')
                    connection.set_result((reader, writer, peer))
                    retained = True
                    return
                require_frame(hello, dict(schema_version=1, action='body_hello', scope=body.scope.wire()))
                await body.ready.wait()
                process = await self.adapter.runner_process(body.reference, singleton=True)
                if peer[0] != process.pid or Process.read(peer[0]) != process or body.streams is not None:
                    raise Refused('planned control caller is not the exact retained MainPID/start/boot/invocation/cgroup')
            body.process, body.streams = process, (reader, writer, peer)
            retained = True
            body.connected.set()
        except (Refused, Unknown, OSError, TimeoutError) as error:
            body.connection_error = str(error) if isinstance(error, (Refused, Unknown)) else type(error).__name__
        finally:
            if not retained:
                writer.close()
                await writer.wait_closed()

    async def _peer(self, body, *, singleton=False):
        if body.streams is None:
            raise Unknown('planned body control has no retained connection')
        _, writer, peer = body.streams
        process = await self.adapter.runner_process(body.reference, singleton=singleton)
        if (process != body.process or peer != peer_credentials(writer.get_extra_info('socket'))
                or peer[0] != process.pid or Process.read(peer[0]) != process):
            raise Refused('planned body MainPID/control peer changed')
        from mop.node.host_phases import verify_runner_image
        from mop.node.host_scope_systemd import read_bytes_at
        proc = open_directory(Path('/proc')/str(process.pid))
        try:
            executable = os.readlink('exe', dir_fd=proc)
            raw = read_bytes_at(proc, 'cmdline', limit=4096)
            if not raw.endswith(b'\0'):
                raise Refused('actual body cmdline is not bounded exact argv')
            argv = tuple(part.decode('ascii') for part in raw[:-1].split(b'\0'))
            verify_runner_image(executable, argv, self.manifest.tools['python'], self.installed.runner, body.scope)
        finally: os.close(proc)
        if Process.read(process.pid) != process:
            raise Refused('body process changed during executable proof')
        await self.adapter.resource_readback(body.scope)

    async def _run(self, body, item):
        try:
            async with asyncio.timeout_at(item.deadline):
                action = item.submission.admission.work.action
                offset = 0
                if action == 'prepare_body':
                    await self.checkpoint(item, 'workspace', 0)
                    parent = open_directory(self.installed.body_root, owner=0, immutable=True)
                    try:
                        os.mkdir(body.scope.launch_id, 0o700, dir_fd=parent)
                        fd = os.open(body.scope.launch_id, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=parent)
                        try:
                            os.fchown(fd, self.installed.body_uid, self.installed.body_gid)
                            v = os.fstat(fd)
                            body.workspace = v.st_dev, v.st_ino
                        finally: os.close(fd)
                    finally: os.close(parent)
                    await self._listener(body)
                    await self.checkpoint(item, 'start-broker', 1)
                    self.adapter.configure_phase(body.scope, self.manifest.project(body.projection))
                    snapshot = await self.execute(self.installed.node_uid, Request('start', body.scope, str(uuid4())))
                    body.reference = snapshot.reference
                    body.ready.set()
                    await body.connected.wait()
                    offset = 2
                await self._peer(body, singleton=True)
                reader, writer, _ = body.streams
                writer.write(item.submission.encode()+b'\n')
                await writer.drain()
                async def body_frame():
                    value = decode(await read_frame(reader))
                    if type(value) is dict and value.get('action') == 'body_failed':
                        exact(value, ('schema_version', 'action', 'attempt_id', 'error'))
                        reason = value['error']
                        if (type(value['schema_version']) is not int or value['schema_version'] != 1
                                or value['attempt_id'] != item.submission.admission.attempt_id
                                or type(reason) is not str or not 0 < len(reason) <= 512 or not reason.isprintable()):
                            raise Refused('invalid finite body failure')
                        raise Unknown('planned body failed: '+reason)
                    return value
                for sequence, effect in enumerate(CHECKPOINTS[action]):
                    if sequence < offset: continue
                    value = await body_frame()
                    require_frame(value, dict(schema_version=1, action='body_checkpoint', effect=effect, sequence=sequence,
                        attempt_id=item.submission.admission.attempt_id))
                    await self._peer(body)
                    await self.checkpoint(item, effect, sequence)
                    # Approval только этому body connection и ровно этому effect.
                    writer.write(encode(dict(schema_version=1, approved=effect, sequence=sequence,
                        attempt_id=item.submission.admission.attempt_id))+b'\n')
                    await writer.drain()
                value = await body_frame()
                require_frame(value, dict(schema_version=1, action='body_complete', attempt_id=item.submission.admission.attempt_id,
                    evidence=CONFIRMATIONS[action]))
                await self._peer(body, singleton=True)
                self._clean_input(item)
                item.status = 'succeeded'
        except (Refused, Unknown, OSError, TimeoutError, asyncio.CancelledError) as error:
            item.status, item.error = 'unknown', str(error) if isinstance(error, (Refused, Unknown)) else (
                'planned '+item.submission.admission.work.action+' interrupted: '+type(error).__name__
                +' at '+(item.pending.effect if item.pending is not None else (item.last_effect or 'body-control'))
                +('; control refusal: '+body.connection_error if body.connection_error else ''))
            try:
                await self._cleanup_phase(body)
            except (Refused, Unknown, OSError, TimeoutError) as cleanup_error:
                item.error += '; cleanup unknown: '+str(cleanup_error)
        except Exception as error:
            item.status, item.error = 'unknown', 'planned phase failed: '+type(error).__name__
            try: await self._cleanup_phase(body)
            except Exception as cleanup_error: item.error += '; cleanup failed: '+type(cleanup_error).__name__

    def _clean_input(self, item):
        if item.receipt is not None:
            self.store.cleanup_input(item.receipt)
            self.store.cleanup_snapshot(item.receipt)
            self.store.forget(item.receipt)
            item.receipt = None

    def _phase_cleanup_task(self, body):
        if body.cleanup is None:
            body.cleanup = asyncio.create_task(self._cleanup_owned(body))
        return body.cleanup

    async def _cleanup_phase(self, body):
        await asyncio.shield(self._phase_cleanup_task(body))

    async def _cleanup_owned(self, body):
        if body.native is not None:
            await body.native.aclose()
        start = self._starts.get(body.scope)
        retired = None
        if start is not None:
            if start.request.action != 'start' or start.request.scope != body.scope:
                raise Unknown('planned cleanup lost its original physical start')
            snapshot = await asyncio.shield(start.task)
            if (snapshot.reference != self._owned.get(body.scope)
                    or body.reference is not None and body.reference != snapshot.reference):
                raise Unknown('planned original physical start reference changed')
            body.reference = snapshot.reference
            body.ready.set()
            if start.cleanup is not None:
                retired = await asyncio.shield(start.cleanup)
                if retired is None or retired.reference != body.reference or retired.populated:
                    raise Unknown('planned original physical retirement outcome unknown')
        if body.reference is not None:
            observation = self.active.get(body.scope)
            if observation is not None and observation is not start:
                # Join уже начатого inspect, не его успех и не его reference.
                await asyncio.wait({observation.task})
            if retired is None:
                facts = await self.adapter.retire(body.reference)
                if facts.verify(self.installed, body.scope, body.reference).populated:
                    raise Unknown('planned cleanup requires exact empty cgroup first')
        elif body.scope in self._fences:
            raise Unknown('planned launch has no retained reference; cleanup unknown')
        for item in body.attempts: self._clean_input(item)
        if body.workspace is not None:
            if body.reference is None:
                # Не было runner: новая пустая root-created entry, не tree traversal.
                pass
            else:
                body.cleanup_connection = asyncio.get_running_loop().create_future()
                await self.adapter.cleanup_workspace(body.reference, body.cleanup_connection, self.manifest.tools['python'])
            parent = open_directory(self.installed.body_root, owner=0, immutable=True)
            try:
                info = os.stat(body.scope.launch_id, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != body.workspace:
                    raise Unknown('planned workspace cleanup inode changed')
                os.rmdir(body.scope.launch_id, dir_fd=parent)
                body.workspace = None
            finally: os.close(parent)
        if body.streams is not None:
            body.streams[1].close()
        if body.listener is not None:
            body.listener.close()
            await body.listener.wait_closed()
        pathname = self.config.control(body.scope)
        if body.socket_identity is not None:
            info = pathname.lstat()
            if not stat.S_ISSOCK(info.st_mode) or (info.st_dev, info.st_ino) != body.socket_identity:
                raise Unknown('planned control socket cleanup inode changed')
            pathname.unlink()
            body.socket_identity = None

    async def handle_planned(self, reader, writer, raw):
        """Короткий finite node exchange, не long-running RPC и не callback."""
        try:
            value = decode(raw)
            action = value.get('action') if type(value) is dict else None
            if action == 'native_exec':
                from mop.node.host_native import NativeSubmission
                item = self.submit_native(NativeSubmission.decode(raw))
                result = self.status(item)
            elif action in ('native_poll', 'native_complete'):
                from mop.node.host_native_owner import mailbox_exchange
                peer = peer_credentials(writer.get_extra_info('socket'))
                if peer[1] != self.installed.node_uid:
                    raise Refused('native mailbox caller is not the actual node UID')
                process = Process.read(peer[0])
                worker = str(process.pid)+':'+str(process.start)+':'+process.boot_id
                result = await mailbox_exchange(self, value, worker)
            elif action in CHECKPOINTS:
                item = self.submit(Submission.decode(raw))
                result = self.status(item)
            elif action == 'phase_ack':
                exact(value, ('schema_version', 'action', 'scope', 'attempt_id', 'receipt', 'committed'))
                item = self._attempt(Scope.parse(value['scope']), value['attempt_id'])
                result_ack = value['committed']
                exact(result_ack, ('receipt_id', 'attempt_id', 'revision', 'outcome', 'evidence'))
                from mop.node.host_scope import canonical
                canonical(result_ack['receipt_id'])
                a = item.submission.admission
                if (type(value['schema_version']) is not int or value['schema_version'] != 1
                        or item.status != 'succeeded' or item.phase_receipt != value['receipt']
                        or result_ack['receipt_id'] != item.phase_receipt
                        or result_ack['attempt_id'] != a.attempt_id or result_ack['revision'] != str(a.revision+1)
                        or result_ack['outcome'] != 'succeeded' or result_ack['evidence'] != CONFIRMATIONS[a.work.action]):
                    raise Refused('planned phase ACK differs from exact succeeded receipt/committed machine result')
                if not item.acknowledged:
                    item.acknowledged = True
                    self.bodies[item.submission.descriptor.scope].completed.append(a.work.action)
                result = self.status(item)
            else:
                scope, attempt = address(value)
                item = self._attempt(scope, attempt)
                if action == 'phase_checkpoint':
                    checkpoint = item.pending
                    if item.status != 'accepted' or checkpoint is None or checkpoint.approved or time.monotonic() >= item.deadline:
                        raise Refused('no pending live planned checkpoint')
                    nonce = checkpoint.challenge(writer)
                    try:
                        writer.write(encode(dict(schema_version=1, status='challenge', challenge=nonce,
                            effect=checkpoint.effect, sequence=checkpoint.sequence, attempt_id=attempt))+b'\n')
                        await writer.drain()
                        async with asyncio.timeout(5.):
                            response = decode(await read_frame(reader))
                        require_frame(response, dict(schema_version=1, action='checkpoint_response', attempt_id=attempt, challenge=nonce))
                        if item.pending is not checkpoint or time.monotonic() >= item.deadline:
                            raise Refused('planned checkpoint response changed or expired')
                        checkpoint.approve(writer, nonce)
                        item.approved.set()
                    finally:
                        checkpoint.disconnect(writer)
                elif action in ('retire_owned', 'cleanup_owned'):
                    body = self.bodies[scope]
                    if item.task is not None and not item.task.done():
                        item.task.cancel()
                    self._phase_cleanup_task(body)
                    # Только submission cleanup; последующий status не начинает эффект.
                result = self.status(item)
            response = dict(schema_version=1, status='ok', phase=result, error=None)
        except (Refused, Unknown) as error:
            response = dict(schema_version=1, status='refused' if isinstance(error, Refused) else 'unknown', phase=None, error=str(error))
        except Exception as error:
            reason = 'planned exchange failed: '+type(error).__name__
            if isinstance(error, ModuleNotFoundError):
                import re
                if type(error.name) is str and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]{0,159}', error.name):
                    reason += ' ('+error.name+')'
            response = dict(schema_version=1, status='unknown', phase=None, error=reason)
        writer.write(encode(response)+b'\n')
        await writer.drain()
        # Только finite transport ACK; ownership phase по нему не передаётся.
        ack = decode(await read_frame(reader))
        require_frame(ack, dict(schema_version=1, action='phase_response_ack'))

    async def aclose(self):
        self._closed = True
        for item in self.attempts.values():
            if item.task is not None and not item.task.done(): item.task.cancel()
        tasks = [item.task for item in self.attempts.values() if item.task is not None]
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        results = await asyncio.gather(*(self._cleanup_phase(body) for body in self.bodies.values()), return_exceptions=True)
        if self._body_handlers: await asyncio.gather(*self._body_handlers, return_exceptions=True)
        try:
            if self.store is not None: self.store.close()
        finally:
            await super().aclose()
        for result in results:
            if isinstance(result, BaseException): raise result
