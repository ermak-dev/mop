"""Peer-authenticated finite supervisor; отмена caller не бросает OS effect.

Нет durable sidecar: только bounded process-local tasks/fences. Новый процесс
не читает старый work/ready/allocation файл и никогда не возобновляет start.
"""
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import socket
import stat
import struct

from mop.node.host_scope import (
    FRAME_LIMIT, Facts, Installed, OsAdapter, Refused, Request, Unknown, decode, encode,
)


def peer_uid(sock):
    if sock is None or sock.family != socket.AF_UNIX:
        raise Refused('Unix peer credentials required')
    return struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


async def read_frame(reader):
    try:
        raw = await reader.readuntil(b'\n')
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as error:
        raise Refused('incomplete or oversized HOST frame') from error
    if len(raw) > FRAME_LIMIT+1:
        raise Refused('HOST frame exceeds limit')
    return raw[:-1]


@dataclass
class _Action:
    request: Request
    task: asyncio.Task
    abandoned: bool = False
    cleanup: asyncio.Task | None = None


class Supervisor:
    def __init__(self, installed: Installed, adapter: OsAdapter, *, capacity=256, timeout=10.):
        if (type(installed) is not Installed or type(capacity) is not int
                or not 1 <= capacity <= 4096 or not .05 <= timeout <= 30):
            raise Refused('bounded installed HOST supervisor required')
        self.installed, self.adapter = installed, adapter
        self.capacity, self.timeout = capacity, timeout
        self.active = {}
        # Завершённый OS start ещё принадлежит root до exact receipt ACK.
        # Inspect может идти параллельно ожиданию ACK, но не cleanup.
        self._receipts = {}
        self._fences = set()
        self._closed = False
        self.last_error = None

    async def _perform(self, request):
        facts = await self.adapter.inspect(request.scope)
        if request.action == 'start':
            if facts is not None:
                raise Refused('HOST unit collision; existing unit cannot replay start')
            try:
                facts = await self.adapter.start(request.scope)
                if type(facts) is not Facts:
                    raise Unknown('HOST start returned no actual OS facts')
                facts.verify(self.installed, request.scope)
            except (Unknown, Refused) as launch_error:
                # OS мог создать unit, но потерять ACK. До возврата unknown
                # пытаемся убрать только точный проверенный собственный scope.
                try:
                    owned = await self.adapter.inspect(request.scope)
                    if owned is not None:
                        snapshot = owned.verify(self.installed, request.scope)
                        empty = await self.adapter.retire(snapshot.reference)
                        if empty.verify(self.installed, request.scope, snapshot.reference).populated:
                            raise Unknown('uncertain HOST launch remains populated')
                except (Refused, Unknown) as cleanup_error:
                    self.last_error = str(cleanup_error)
                raise Unknown('HOST launch uncertain: '+str(launch_error)) from launch_error
        else:
            if facts is None:
                raise Unknown('HOST unit absent; physical outcome unknown')
            facts.verify(self.installed, request.scope, request.reference)
            if request.action == 'retire':
                facts = await self.adapter.retire(request.reference)
        if type(facts) is not Facts:
            raise Unknown('HOST adapter returned no actual OS facts')
        snapshot = facts.verify(self.installed, request.scope, request.reference)
        if request.action == 'retire' and snapshot.populated:
            raise Unknown('HOST containment still populated; retirement unknown')
        return snapshot

    async def execute(self, uid, request, *, receipt=False):
        if type(uid) is not int or uid != self.installed.node_uid:
            raise Refused('HOST supervisor peer is not the installed node-service UID')
        if type(request) is not Request:
            raise Refused('typed finite HOST request required')
        if self._closed:
            raise Refused('HOST supervisor is closing')
        pending = self._receipts.get(request.scope)
        if request.scope in self.active or pending is not None and pending.abandoned:
            raise Refused('HOST scope action already active')
        owned_scopes = self.active.keys() | self._receipts.keys()
        if request.scope not in owned_scopes and len(owned_scopes) >= self.capacity:
            raise Refused('HOST supervisor busy; action capacity exhausted')
        if request.action == 'start':
            if request.scope in self._fences:
                raise Refused('HOST start replay or uncertain previous action')
            if len(self._fences) >= self.capacity:
                raise Refused('HOST start fence capacity exhausted; reconciliation required')
            # Fences не вытесняются: неизвестный эффект нельзя превратить в retry.
            self._fences.add(request.scope)
        action = _Action(request, asyncio.create_task(self._perform(request)))
        self.active[request.scope] = action
        try:
            async with asyncio.timeout(self.timeout):
                snapshot = await asyncio.shield(action.task)
                if receipt and request.action == 'start':
                    self._receipts[request.scope] = action
                return snapshot
        except (TimeoutError, asyncio.CancelledError) as error:
            self._abandon(action)
            if isinstance(error, asyncio.CancelledError):
                raise
            raise Unknown('HOST action deadline expired; outcome unknown') from error
        finally:
            if not action.abandoned:
                self.active.pop(request.scope, None)

    def _abandon(self, action, predecessor=None):
        action.abandoned = True
        if action.cleanup is None:
            action.cleanup = asyncio.create_task(self._cleanup(action, predecessor))

    async def _cleanup(self, action, predecessor):
        try:
            if predecessor is not None and predecessor is not action:
                # Ждём конец уже начатого OS effect, не его успех. Ошибка
                # inspect не отменяет независимую exact-owned cleanup proof.
                await asyncio.wait({predecessor.task})
            snapshot = await action.task
            if action.request.action == 'start':
                facts = await self.adapter.inspect(snapshot.reference.scope)
                if facts is None:
                    raise Unknown('abandoned HOST start absent; cleanup unknown')
                facts.verify(self.installed, snapshot.reference.scope, snapshot.reference)
                empty = await self.adapter.retire(snapshot.reference)
                if empty.verify(self.installed, snapshot.reference.scope, snapshot.reference).populated:
                    raise Unknown('abandoned HOST start remains populated')
        except (Refused, Unknown) as error:
            self.last_error = str(error)
        except asyncio.CancelledError:
            self.last_error = 'HOST cleanup cancelled; outcome unknown'
            raise
        except Exception as error:
            self.last_error = 'HOST cleanup failed: ' + type(error).__name__
        finally:
            address = action.request.scope
            if self.active.get(address) is action:
                self.active.pop(address)
            if self._receipts.get(address) is action:
                self._receipts.pop(address)

    def _receipt(self, request, snapshot):
        action = self._receipts.get(request.scope)
        if (action is None or action.request != request
                or action.task.result() != snapshot):
            raise Unknown('HOST start receipt ownership mismatch; cleanup unknown')
        return action

    def acknowledge(self, request, snapshot):
        """Только exact peer ACK передаёт ownership успешного start caller."""
        if request.action == 'start':
            action = self._receipt(request, snapshot)
            if action.abandoned:
                raise Unknown('HOST start receipt already abandoned; cleanup active')
            self._receipts.pop(request.scope)

    async def abandon_started(self, request, snapshot):
        """Ответ не подтверждён peer ACK; exact cleanup ждёт текущий inspect."""
        if request.action != 'start':
            return
        action = self._receipt(request, snapshot)
        self._abandon(action, self.active.get(request.scope))
        await asyncio.shield(action.cleanup)

    async def aclose(self):
        self._closed = True
        actions = tuple(self.active.values()) + tuple(self._receipts.values())
        for action in actions:
            self._abandon(action, self.active.get(action.request.scope))
        tasks = {a.cleanup for a in actions if a.cleanup is not None}
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.timeout)
            if pending:
                raise Unknown('HOST supervisor cleanup incomplete; outcome unknown')
        if self.last_error:
            raise Unknown(self.last_error)


async def serve(installed, path, stop):
    """Production daemon entry для будущего installer composition.

    Никаких env/config.json/credentials: только проверенный root-installed
    descriptor, root-owned socket directory и signal-owned stop event.
    """
    from mop.node.host_scope_systemd import SystemdCgroupAdapter
    adapter = SystemdCgroupAdapter(installed)
    owner = Supervisor(installed, adapter)
    server = SocketServer(owner, path)
    try:
        await server.start()
        await stop.wait()
    finally:
        try:
            await server.aclose()
        finally:
            await adapter.aclose()


class SocketServer:
    """Один frame и receipt ACK, bounded peers, timeout и закрываемый listener.

    Production adapter допускается только в root-процессе. Сам transport можно
    проверить с fake adapter непривилегированно: это не OS containment proof.
    """
    def __init__(self, supervisor, path, *, timeout=2., connections=8):
        if (not isinstance(path, Path) or not path.is_absolute() or '..' in path.parts
                or len(os.fsencode(path)) > 100 or not .05 <= timeout <= 30
                or type(connections) is not int or not 1 <= connections <= 16):
            raise Refused('bounded installed HOST socket required')
        self.supervisor, self.path = supervisor, path
        self.timeout, self.connections = timeout, connections
        self._server = None
        self._handlers = set()
        self._cleanup = set()
        self._identity = None
        self._closed = False
        self.last_error = None

    async def start(self):
        if self._server is not None or self._closed:
            raise Refused('HOST listener already started or closed')
        # No-follow каждый компонент, иначе writable ancestor подменит endpoint.
        from mop.node.host_scope_systemd import open_directory
        fd = open_directory(self.path.parent, owner=os.geteuid(), private=False,
                            immutable=os.geteuid() == 0)
        try:
            if os.path.lexists(self.path):
                raise Refused('HOST socket collision; no foreign unlink')
            self._server = await asyncio.start_unix_server(self._connected,
                path=str(self.path), limit=FRAME_LIMIT+1, backlog=self.connections)
            os.chown(self.path, self.supervisor.installed.node_uid, -1)
            os.chmod(self.path, 0o600)
            info = os.stat(self.path, follow_symlinks=False)
            self._identity = (info.st_dev, info.st_ino)
            self._parent_fd = fd
            fd = None
        finally:
            if fd is not None:
                os.close(fd)

    def _connected(self, reader, writer):
        if self._closed or len(self._handlers) >= self.connections:
            writer.close()
            return
        task = asyncio.create_task(self._handle(reader, writer))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _handle(self, reader, writer):
        request = snapshot = None
        acknowledged = False
        try:
            uid = peer_uid(writer.get_extra_info('socket'))
            if uid != self.supervisor.installed.node_uid:
                raise Refused('HOST socket peer is not the installed node-service UID')
            async with asyncio.timeout(self.timeout):
                request = Request.decode(await read_frame(reader))
            snapshot = await self.supervisor.execute(uid, request, receipt=True)
            async with asyncio.timeout(self.timeout):
                writer.write(encode(dict(schema_version=1, request_id=request.request_id,
                    status='ok', snapshot=snapshot.wire(), error=None))+b'\n')
                await writer.drain()
                ack = decode(await read_frame(reader))
                if ack != dict(schema_version=1, ack=request.request_id) or type(ack.get('schema_version')) is not int:
                    raise Refused('exact HOST receipt ACK required')
                self.supervisor.acknowledge(request, snapshot)
                acknowledged = True
        except (Refused, Unknown) as error:
            self.last_error = str(error)
            if request is not None and snapshot is None:
                try:
                    async with asyncio.timeout(self.timeout):
                        writer.write(encode(dict(schema_version=1, request_id=request.request_id,
                            status='refused' if isinstance(error, Refused) else 'unknown',
                            snapshot=None, error=str(error)))+b'\n')
                        await writer.drain()
                except (OSError, TimeoutError):
                    pass
        except (OSError, TimeoutError, asyncio.CancelledError):
            self.last_error = 'HOST peer disconnected or timed out; outcome unknown'
        except Exception as error:
            self.last_error = 'HOST action failed; outcome unknown: ' + type(error).__name__
        finally:
            if snapshot is not None and not acknowledged and request.action == 'start':
                cleanup = asyncio.create_task(self.supervisor.abandon_started(request, snapshot))
                self._cleanup.add(cleanup)
                cleanup.add_done_callback(self._cleanup_done)
            writer.close()
            try:
                async with asyncio.timeout(self.timeout):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                pass

    def _cleanup_done(self, task):
        self._cleanup.discard(task)
        if not task.cancelled() and task.exception():
            self.last_error = 'HOST lost-ACK cleanup incomplete; outcome unknown'

    async def drain(self):
        tasks = self._handlers | self._cleanup
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.supervisor.timeout+self.timeout*2)
            if pending:
                raise Unknown('HOST socket actions still active; outcome unknown')
        if self._cleanup:
            await asyncio.wait(self._cleanup, timeout=self.supervisor.timeout)

    async def aclose(self):
        self._closed = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for task in tuple(self._handlers):
            task.cancel()
        try:
            await self.drain()
            await self.supervisor.aclose()
        finally:
            if self._identity is not None:
                try:
                    info = os.stat(self.path.name, dir_fd=self._parent_fd, follow_symlinks=False)
                    if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == self._identity:
                        os.unlink(self.path.name, dir_fd=self._parent_fd)
                except FileNotFoundError:
                    pass
                os.close(self._parent_fd)
                self._identity = None
