"""Node-service client; HTTP bearer остаётся здесь, не у root/body.

Admission guard обязателен для каждого обращения, включая inspect/retire.
Нет автоматических retry или восстановления start из старого файла.
"""
import asyncio
import os
from pathlib import Path
from uuid import uuid4

from mop.node.host_scope import (
    Refused, Unknown, Request, Snapshot, decode, encode, exact,
)
from mop.node.host_scope_supervisor import peer_uid, read_frame
from mop.node.http_execution import EffectGuard, Work


class UnboundHostScopes:
    async def start(self, scope, guard):
        raise Unknown('physical HOST containment supervisor is not bound')

    async def inspect(self, reference, guard):
        raise Unknown('physical HOST containment supervisor is not bound')

    async def retire(self, reference, guard):
        raise Unknown('physical HOST containment supervisor is not bound')


class Client:
    def __init__(self, path, *, node_uid, supervisor_uid=0, timeout=10., work=None):
        if (not isinstance(path, Path) or not path.is_absolute() or '..' in path.parts
                or len(os.fsencode(path)) > 100 or type(node_uid) is not int or node_uid <= 0
                or type(supervisor_uid) is not int or supervisor_uid < 0 or not .05 <= timeout <= 30):
            raise Refused('installed bounded HOST socket client required')
        self.path, self.node_uid = path, node_uid
        self.supervisor_uid, self.timeout = supervisor_uid, timeout
        if work is not None and type(work) is not Work:
            raise Refused('typed current work binding required')
        self.work = work

    async def start(self, scope, guard):
        return await self._request(Request('start', scope, str(uuid4())), guard)

    async def inspect(self, reference, guard):
        return await self._request(Request('inspect', reference.scope, str(uuid4()), reference), guard)

    async def retire(self, reference, guard):
        return await self._request(Request('retire', reference.scope, str(uuid4()), reference), guard)

    def _check(self, request, guard):
        guard.check()
        address = ('epoch', 'node_id', 'executor_epoch', 'target_id', 'incarnation_id')
        if self.work is None or any(getattr(request.scope, k) != getattr(self.work, k) for k in address):
            raise Refused('HOST scope address does not match current authenticated work')
        self.work.gate(node_id=self.work.node_id, epoch=self.work.epoch,
                       executor_epoch=self.work.executor_epoch)

    async def _request(self, request, guard):
        if type(guard) is not EffectGuard:
            raise Refused('current authenticated server admission guard required')
        # Никакого socket до server recheck. После каждого await локальный fence.
        try:
            async with asyncio.timeout(self.timeout):
                await guard()
        except TimeoutError as error:
            raise Unknown('HOST current admission deadline expired') from error
        self._check(request, guard)
        if os.geteuid() != self.node_uid:
            raise Refused('HOST client is not the installed node-service UID')
        writer = None
        try:
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_unix_connection(str(self.path), limit=4097)
                if peer_uid(writer.get_extra_info('socket')) != self.supervisor_uid:
                    raise Refused('HOST supervisor peer UID mismatch')
                self._check(request, guard)
                writer.write(request.encode()+b'\n')
                await writer.drain()
                value = decode(await read_frame(reader))
                exact(value, ('schema_version', 'request_id', 'status', 'snapshot', 'error'))
                if (type(value['schema_version']) is not int or value['schema_version'] != 1
                        or value['request_id'] != request.request_id):
                    raise Refused('foreign HOST response')
                if value['status'] in ('refused', 'unknown'):
                    reason = value['error']
                    if (value['snapshot'] is not None or type(reason) is not str
                            or not 0 < len(reason) <= 512 or not reason.isprintable()):
                        raise Unknown('invalid HOST refusal; outcome unknown')
                    raise (Refused if value['status'] == 'refused' else Unknown)(reason)
                if value['status'] != 'ok' or value['error'] is not None:
                    raise Unknown('unsupported HOST response; outcome unknown')
                snapshot = Snapshot.parse(value['snapshot'])
                if (snapshot.reference.scope != request.scope
                        or request.reference is not None and snapshot.reference != request.reference
                        or request.action == 'retire' and snapshot.populated):
                    raise Refused('HOST response physical reference mismatch')
                self._check(request, guard)
                writer.write(encode(dict(schema_version=1, ack=request.request_id))+b'\n')
                await writer.drain()
                return snapshot
        except (OSError, TimeoutError) as error:
            raise Unknown('HOST socket connection lost; outcome unknown, never retry start') from error
        finally:
            if writer is not None:
                writer.close()
                try:
                    async with asyncio.timeout(.2):
                        await writer.wait_closed()
                except (OSError, TimeoutError):
                    pass
