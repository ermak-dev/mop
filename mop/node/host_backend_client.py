"""Конкретный node bridge к живому root supervisor, не injectable proof callback."""
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import stat
from uuid import uuid4

from mop.node.host_backend import (
    BackendRequest, BackendState, Identity, Process, VerifiedAuthority, _VERIFIED,
)
from mop.node.host_scope import Refused, Unknown, decode, exact
from mop.node.host_scope_supervisor import peer_credentials, read_frame
from mop.node.host_scope_systemd import open_directory


class BackendClient:
    def __init__(self, path, *, node_uid, identity, timeout=5.):
        if (not isinstance(path, Path) or not path.is_absolute() or '..' in path.parts
                or len(os.fsencode(path)) > 100 or type(node_uid) is not int or node_uid <= 0
                or type(identity) is not Identity or not .05 <= timeout <= 5.):
            raise Refused('installed bounded root HOST backend client required')
        self.path, self.node_uid, self.identity = path, node_uid, identity
        self.timeout = timeout

    @asynccontextmanager
    async def connect(self):
        if os.geteuid() != self.node_uid:
            raise Refused('HOST backend client is not the installed node-service UID')
        writer = None
        session = None
        try:
            async with asyncio.timeout(self.timeout):
                # Endpoint нельзя подменить node/body UID через writable ancestor.
                fd = open_directory(self.path.parent, owner=0, immutable=True)
                try:
                    info = os.stat(self.path.name, dir_fd=fd, follow_symlinks=False)
                    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.node_uid
                            or stat.S_IMODE(info.st_mode) != 0o600):
                        raise Refused('installed HOST backend socket identity mismatch')
                    reader, writer = await asyncio.open_unix_connection(str(self.path), limit=4097)
                    peer = peer_credentials(writer.get_extra_info('socket'))
                    if peer[1] != 0:
                        raise Refused('HOST backend authority requires the root Unix peer')
                    after = os.stat(self.path.name, dir_fd=fd, follow_symlinks=False)
                    if (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino):
                        raise Refused('HOST backend socket changed during connect')
                    session = _Session(self, reader, writer, peer, Process.read(peer[0]))
                    await session.query()
                finally:
                    os.close(fd)
            yield session
        except (OSError, TimeoutError) as error:
            raise Unknown('HOST backend authority connection unavailable; no automatic retry') from error
        finally:
            if session is not None:
                session.closed = True
            if writer is not None:
                writer.close()
                try:
                    async with asyncio.timeout(.2):
                        await writer.wait_closed()
                except (OSError, TimeoutError):
                    pass


class _Session:
    def __init__(self, client, reader, writer, peer, process):
        self.client, self.reader, self.writer = client, reader, writer
        self.peer, self.process = peer, process
        self.closed = False
        self.authority = None

    def check(self, proof):
        if (self.closed or self.writer.is_closing() or self.reader.at_eof()
                or proof is not self.authority or type(proof) is not VerifiedAuthority
                or peer_credentials(self.writer.get_extra_info('socket')) != self.peer
                or self.peer[1] != 0 or Process.read(self.peer[0]) != self.process):
            raise Refused('HOST authority is not bound to the current live root connection')

    async def query(self):
        return await self._request('backend_query')

    async def bind(self, *, epoch, executor_epoch, empty):
        return await self._request('backend_bind', epoch, executor_epoch, empty)

    async def confirm(self, *, epoch, executor_epoch, empty):
        return await self._request('backend_confirm', epoch, executor_epoch, empty)

    async def planned(self, request, *, guard=None):
        """Только finite phase exchange поверх уже pinned root authority session."""
        from mop.node.host_scope import encode
        from mop.node.host_phases import Submission, address
        from mop.node.host_native import NativeSubmission
        from mop.node.http_execution import EffectGuard
        self.check(self.authority)
        if type(request) in (Submission, NativeSubmission):
            raw = request.encode()
        else:
            if type(request) is not dict:
                raise Refused('finite planned request required')
            if request.get('action') in ('native_poll', 'native_complete'):
                from mop.node.host_native_lifetime import mailbox_request
                mailbox_request(request)
            elif request.get('action') != 'phase_ack':
                address(request)
            raw = encode(request)
        async with asyncio.timeout(self.client.timeout):
            self.writer.write(raw+b'\n')
            await self.writer.drain()
            value = decode(await read_frame(self.reader))
            self.check(self.authority)
            if value.get('status') == 'challenge':
                exact(value, ('schema_version', 'status', 'challenge', 'effect', 'sequence', 'attempt_id'))
                from mop.node.host_scope import canonical
                if type(value['schema_version']) is not int or value['schema_version'] != 1:
                    raise Refused('unsupported planned checkpoint challenge schema')
                canonical(value['challenge'])
                if (type(guard) is not EffectGuard or guard.admission is None
                        or guard.admission.attempt_id != value['attempt_id']
                        or request.get('action') != 'phase_checkpoint'
                        or value['attempt_id'] != request['attempt_id']):
                    raise Refused('planned checkpoint requires its actual Runtime Admission guard')
                from mop.node.host_phases import checkpoint_plan
                plan = checkpoint_plan(guard.admission.work.action)
                if (type(value['sequence']) is not int or not 0 <= value['sequence'] < len(plan)
                        or plan[value['sequence']] != value['effect']):
                    raise Refused('planned checkpoint effect/sequence changed')
                await guard()
                guard.check()
                self.check(self.authority)
                self.writer.write(encode(dict(schema_version=1, action='checkpoint_response',
                    attempt_id=value['attempt_id'], challenge=value['challenge']))+b'\n')
                await self.writer.drain()
                value = decode(await read_frame(self.reader))
                self.check(self.authority)
        self.writer.write(encode(dict(schema_version=1, action='phase_response_ack'))+b'\n')
        await self.writer.drain()
        exact(value, ('schema_version', 'status', 'phase', 'error'))
        if type(value['schema_version']) is not int or value['schema_version'] != 1:
            raise Refused('invalid planned HOST response schema')
        if value['status'] in ('refused', 'unknown'):
            error = value['error']
            if type(error) is not str or not 0 < len(error) <= 1024 or not error.isprintable():
                raise Refused('invalid planned HOST refusal')
            raise (Refused if value['status'] == 'refused' else Unknown)(error)
        if value['status'] != 'ok' or value['error'] is not None or type(value['phase']) is not dict:
            raise Refused('invalid planned HOST response status')
        return value['phase']

    async def native_mailbox(self, request):
        from mop.node.host_native_lifetime import mailbox_request
        mailbox_request(request)
        return await self.planned(request)

    async def _request(self, action, epoch=None, executor_epoch=None, empty=None):
        if self.authority is not None:
            self.check(self.authority)
        request = BackendRequest(action, str(uuid4()), self.client.identity, epoch, executor_epoch, empty)
        async with asyncio.timeout(self.client.timeout):
            self.writer.write(request.encode()+b'\n')
            await self.writer.drain()
            value = decode(await read_frame(self.reader))
        exact(value, ('schema_version', 'request_id', 'status', 'authority', 'error'))
        if (type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['request_id'] != request.request_id):
            raise Refused('foreign HOST backend authority response')
        if value['status'] in ('refused', 'unknown'):
            error = value['error']
            if (value['authority'] is not None or type(error) is not str
                    or not 0 < len(error) <= 512 or not error.isprintable()):
                raise Refused('invalid HOST backend refusal')
            raise (Refused if value['status'] == 'refused' else Unknown)(error)
        if value['status'] != 'ok' or value['error'] is not None:
            raise Refused('invalid HOST backend authority status')
        state = BackendState.parse(value['authority'])
        if (state.identity != self.client.identity or state.process != self.process
                or self.authority is not None and state.executor_epoch != self.authority.state.executor_epoch
                or action == 'backend_query' and state.mode != 'query'
                or action != 'backend_query' and state.bound_epoch != epoch
                or action == 'backend_confirm' and (state.mode != 'resume' or state.executor_epoch != executor_epoch)):
            raise Refused('HOST backend authority identity/process/epoch mismatch')
        self.authority = VerifiedAuthority(state, self, _proof=_VERIFIED)
        self.check(self.authority)
        return self.authority
