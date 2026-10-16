"""Async hook → HTTP bridge. Только память, ограниченные tasks/bytes/time.

Root #643 владеет этим runtime и выдаёт body environment после authoritative
session binding. Остановка bridge закрывает только свои сокеты/tasks, не CLI.
"""
import asyncio
from collections import deque
import hmac
import json
import os
from pathlib import Path
import socket
import stat
import struct
from time import monotonic
from uuid import UUID, uuid4

from mop.common.telemetry import (Ack, MAX_RECORD, Observation,
    SessionScope, canonical, counter, fields, text, uuid)
from mop.node.http_execution import Refused, Unavailable, _unique_object


class TelemetryClient:
    def __init__(self, transport):
        self.transport = transport

    async def ingest(self, observation):
        if type(observation) is not Observation:
            raise Refused('typed telemetry required')
        value = await self.transport.request('POST', 'telemetry', observation.wire())
        fields(value, ('schema_version', 'producer_epoch', 'sequence'))
        if type(value['schema_version']) is not int or value['schema_version'] != 1:
            raise Refused('unsupported telemetry acknowledgement')
        result = Ack(uuid(value['producer_epoch']), counter(value['sequence']))
        if result != Ack(observation.scope.producer_epoch, observation.sequence):
            raise Refused('foreign telemetry acknowledgement')
        return result


class HookBridge:
    def __init__(self, client, scope, *, path, token, capacity=128, interval=.5,
                 io_timeout=.2, http_timeout=5.0, connections=8, allowed_uid=None, age_limit=30.0):
        if (type(scope) is not SessionScope or type(capacity) is not int or not 1 <= capacity <= 1024
                or not .01 <= interval <= 10 or not .01 <= io_timeout <= 1
                or not .01 <= http_timeout <= 10 or not .01 <= age_limit <= 60
                or type(connections) is not int or not 1 <= connections <= 16
                or not text(token, 64) or len(token) != 64
                or type(path) is not str or not os.path.isabs(path) or len(os.fsencode(path)) > 100):
            raise ValueError('bounded addressed hook bridge required')
        self.client, self.scope = client, scope
        self.path, self._token = path, token
        self.capacity, self.interval, self.age_limit = capacity, interval, age_limit
        self.io_timeout, self.http_timeout, self.connections = io_timeout, http_timeout, connections
        self.allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        self._queue = deque()
        self._sequence = 0
        self._turn = None
        self._prompt_binding = None
        self.dropped = 0
        self._pending_drop = 0
        self._server = None
        self._worker = None
        self._handlers = set()
        self._writers = set()
        self._lock = asyncio.Lock()
        self._closed = False
        self._started = False
        self.last_error = None
        self._socket_identity = None

    @property
    def buffered(self):
        return len(self._queue)

    @property
    def pending_tasks(self):
        return tuple(task for task in (*self._handlers, self._worker) if task is not None and not task.done())

    def environment(self):
        return dict(MOP_TELEMETRY_SOCKET=self.path, MOP_TELEMETRY_TOKEN=self._token)

    def bind_processed_turn(self, turn_id: UUID):
        """Trusted adapter связывает реально обрабатываемый ход с delivery ref.

        Queue acceptance/send/текущий owner не доказывают начало этого хода.
        Без подтверждённого processing root не вызывает port: attribution
        остаётся unknown. Hook JSON не имеет доступа к этому методу.
        """
        if self._closed or type(turn_id) is not UUID:
            raise ValueError('live typed processed-turn correlation required')
        self._turn = turn_id
        self._prompt_binding = turn_id

    def accept(self, record, *, token):
        if self._closed or type(token) is not str or not hmac.compare_digest(token, self._token):
            return False
        try:
            fields(record, ('schema_version', 'session_id', 'event', 'model', 'error'))
            if (type(record['schema_version']) is not int or record['schema_version'] != 1
                    or record['session_id'] != self.scope.session_id or len(canonical(record)) > MAX_RECORD):
                return False
            event = record['event']
            # Attribution никогда не приходит из caller JSON. Непривязанный ход
            # остаётся unknown, а не приписывается нынешнему owner папета.
            turn = (self._prompt_binding or uuid4()) if event == 'UserPromptSubmit' else self._turn
            row = Observation(self.scope, self._sequence+1, event, turn, record['model'], record['error'],
                              None, self._pending_drop)
        except (ValueError, TypeError, OverflowError):
            return False
        self._sequence += 1
        self._turn = turn
        if event in ('UserPromptSubmit', 'Stop', 'StopFailure'):
            self._prompt_binding = None
        if len(self._queue) >= self.capacity:
            self.dropped += 1
            self._pending_drop += 1
            return False
        self._pending_drop = 0
        self._queue.append((row, monotonic()))
        return True

    async def flush(self):
        async with self._lock:
            for _ in range(self.capacity):
                if self._closed or not self._queue:
                    return
                row, queued_at = self._queue[0]
                if monotonic()-queued_at > self.age_limit:
                    self._queue.popleft()
                    self.dropped += 1
                    self._pending_drop += 1+row.dropped
                    continue
                try:
                    async with asyncio.timeout(self.http_timeout):
                        ack = await self.client.ingest(row)
                    if ack != Ack(row.scope.producer_epoch, row.sequence):
                        raise Refused('uncommitted or foreign telemetry acknowledgement')
                except (OSError, TimeoutError, ValueError, Unavailable, Refused) as error:
                    # Raw exception/credential не попадает в telemetry или логи.
                    self.last_error = 'telemetry not acknowledged: ' + type(error).__name__
                    return
                self._queue.popleft()
                self.last_error = None

    async def start(self):
        if self._closed or self._started:
            raise ValueError('hook bridge already started or closed')
        parent = Path(self.path).parent
        info = parent.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError('private owned runtime directory required')
        if os.path.lexists(self.path):
            raise ValueError('hook endpoint already exists')
        self._server = await asyncio.start_unix_server(self._connected, path=self.path, limit=MAX_RECORD,
                                                      backlog=self.connections)
        os.chmod(self.path, 0o600)
        info = os.stat(self.path)
        self._socket_identity = (info.st_dev, info.st_ino)
        self._started = True
        self._worker = asyncio.create_task(self._run())

    def _connected(self, reader, writer):
        if self._closed or len(self._handlers) >= self.connections:
            writer.close()
            return
        self._writers.add(writer)
        task = asyncio.create_task(self._receive(reader, writer))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _receive(self, reader, writer):
        try:
            sock = writer.get_extra_info('socket')
            if not hasattr(socket, 'SO_PEERCRED'):
                raise ValueError('Unix peer credentials unavailable')
            _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != self.allowed_uid:
                return
            async with asyncio.timeout(self.io_timeout):
                data = await reader.readline()
            if not data.endswith(b'\n') or len(data) > MAX_RECORD:
                return
            value = json.loads(data, object_pairs_hook=_unique_object)
            fields(value, ('token', 'record'))
            self.accept(value['record'], token=value['token'])
        except (ValueError, TypeError, OSError, TimeoutError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()
            try:
                async with asyncio.timeout(self.io_timeout):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                pass
            self._writers.discard(writer)

    async def _run(self):
        while not self._closed:
            await self.flush()
            await asyncio.sleep(self.interval)

    async def aclose(self):
        self._closed = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for writer in tuple(self._writers):
            writer.close()
        tasks = self.pending_tasks
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Никакой durable offline гарантии: смерть runtime теряет эту память.
        self.dropped += len(self._queue)
        self._queue.clear()
        if self._socket_identity is not None:
            try:
                info = os.stat(self.path, follow_symlinks=False)
                if (info.st_dev, info.st_ino) == self._socket_identity:
                    os.unlink(self.path)
            except FileNotFoundError:
                pass


class TelemetryRuntime:
    """Root-owned bounded lifetime; transport принадлежит root, не каждому hook."""
    def __init__(self, transport, *, limit=128):
        if type(limit) is not int or not 1 <= limit <= 512:
            raise ValueError('bounded session runtime required')
        self.client = TelemetryClient(transport)
        self.limit = limit
        self._bridges = {}
        self._starts = {}
        self._closed = False

    async def attach(self, scope, *, path, token):
        if self._closed or len(self._bridges) >= self.limit or scope.target_id in self._bridges:
            raise ValueError('session bridge capacity or lifecycle conflict')
        bridge = HookBridge(self.client, scope, path=path, token=token)
        # Резервируем до await; второй attach не оставит бесхозный Unix server.
        self._bridges[scope.target_id] = bridge
        start = asyncio.create_task(bridge.start())
        self._starts[scope.target_id] = start
        try:
            await start
            if self._closed:
                raise Unavailable('telemetry runtime closed during session attach')
        except BaseException:
            start.cancel()
            await asyncio.gather(start, return_exceptions=True)
            await bridge.aclose()
            self._bridges.pop(scope.target_id, None)
            raise
        finally:
            self._starts.pop(scope.target_id, None)
        return bridge.environment()

    def bind_processed_turn(self, scope: SessionScope, turn_id: UUID) -> None:
        bridge = self._bridges.get(scope.target_id)
        if self._closed or bridge is None or bridge.scope != scope or scope.target_id in self._starts:
            raise Unavailable('actual current session bridge is unavailable')
        bridge.bind_processed_turn(turn_id)

    async def detach(self, target_id):
        bridge = self._bridges.get(target_id)
        start = self._starts.get(target_id)
        if start is not None:
            start.cancel()
            await asyncio.gather(start, return_exceptions=True)
        if bridge is not None:
            await bridge.aclose()
            if self._bridges.get(target_id) is bridge:
                self._bridges.pop(target_id, None)

    async def aclose(self):
        self._closed = True
        starts = tuple(self._starts.values())
        for task in starts:
            task.cancel()
        if starts:
            await asyncio.gather(*starts, return_exceptions=True)
        for target in tuple(self._bridges):
            await self.detach(target)
