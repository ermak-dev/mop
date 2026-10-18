"""Stdlib bridge только к явно переданному собственному CLI socket.

OwnSession выдаёт trusted локальный launcher; API/grant не задаёт path/pid.
Нет discovery, registry, transcript, primary state-файлов или terminal input.
Временный callback socket — transport, удаляется при shutdown. Общий OS uid
не является sandbox: bridge не обещает изоляцию процессов с одинаковым uid.
"""
import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
from uuid import uuid4

from mop import session
from mop.common.endpoint import ApplicationAcceptance, DeliveryFrame


@dataclass(frozen=True, repr=False)
class OwnSession:
    """Полный proof от trusted launcher, не конструктор из входящего JSON.

    Native conversation подтверждает launcher, не legacy registry. Physical
    snapshot сам по себе conversation не доказывает: /clear сохраняет PID/start.
    Неполная прежняя форма намеренно не поддерживается. Production root пока
    отсутствует; наличие этого типа не включает private inbox capability.
    """
    socket_path: str
    pid: int
    external_session_id: str
    process_start: str
    uid: int
    namespaces: tuple[tuple[str, int], ...]
    cwd: Path
    token: str | None = field(default=None, repr=False)

    def __post_init__(self):
        if (type(self.socket_path) is not str or not os.path.isabs(self.socket_path)
                or '\x00' in self.socket_path or os.path.normpath(self.socket_path) != self.socket_path
                or type(self.pid) is not int or self.pid <= 0
                or type(self.external_session_id) is not str
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', self.external_session_id)
                or type(self.process_start) is not str
                or not re.fullmatch(r'[0-9a-f-]{36}:[0-9]+', self.process_start)
                or type(self.uid) is not int or self.uid < 0
                or type(self.namespaces) is not tuple or len(self.namespaces) != 3
                or any(type(pair) is not tuple or len(pair) != 2
                    or pair[0] != name or type(pair[1]) is not int or pair[1] <= 0
                    for pair, name in zip(self.namespaces, ('mnt', 'pid', 'user')))
                or not isinstance(self.cwd, Path) or not self.cwd.is_absolute()
                or '..' in self.cwd.parts):
            raise ValueError('complete trusted own physical session proof required')

    def check_process(self, pid, uid):
        """Проверка текущего процесса из connected SO_PEERCRED, не новый connect."""
        if pid != self.pid or uid != self.uid or uid != os.getuid():
            raise PermissionError('own CLI peer identity changed')
        try:
            # Stdlib helper окружает process snapshot повторным start/namespace
            # чтением. Он не вызывает discovery/history и не верит файлам сессий.
            current = session.native_process(pid)
            local_namespaces = tuple((kind, Path(f'/proc/self/ns/{kind}').stat().st_ino)
                                     for kind in ('mnt', 'pid', 'user'))
            if (current['start'] != self.process_start or current['uid'] != self.uid
                    or current['cwd'] != str(self.cwd) or current['namespaces'] != self.namespaces
                    or self.namespaces != local_namespaces):
                raise PermissionError('own CLI process start, namespace or cwd changed')
        except (OSError, LookupError, ValueError) as error:
            raise PermissionError('own CLI physical session proof is unavailable or changed') from error


def _unique_object(pairs):
    """Неоднозначный JSON не может быть свидетельством принятия."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate callback field')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('nonfinite callback value')


class LocalBridge:
    def __init__(self, own, *, timeout=2):
        if type(own) is not OwnSession or type(own.pid) is not int or own.pid <= 0:
            raise ValueError('trusted own external session required')
        if not isinstance(own.external_session_id, str) or not own.external_session_id or len(own.external_session_id) > 256:
            raise ValueError('own external conversation identity required')
        if not 0 < timeout <= 5:
            raise ValueError('local bridge timeout must be within 5 seconds')
        self.own, self.timeout = own, timeout
        self.external_session_id = own.external_session_id
        self._server = None
        self._callback = None
        self._pending = {}
        self._idle = {}
        self._seen = {}
        self._tasks = set()
        self._outbound = set()
        self._writers = set()
        self._stopped = False

    def _path(self):
        path = Path(self.own.socket_path)
        if not path.is_absolute() or str(path.resolve()) != str(path):
            raise PermissionError('own CLI socket must be an explicit canonical path')
        directory = path.parent.stat()
        target = path.lstat()
        if (directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) & 0o077
                or target.st_uid != os.getuid() or not stat.S_ISSOCK(target.st_mode)
                or stat.S_IMODE(target.st_mode) & 0o077):
            raise PermissionError('own CLI socket requires private ownership and directory')
        return path

    def _peer(self, writer):
        sock = writer.get_extra_info('socket')
        if not hasattr(socket, 'SO_PEERCRED') or sock is None:
            raise PermissionError('own CLI peer identity is unavailable')
        pid, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
        self.own.check_process(pid, uid)

    async def ready(self):
        if self._stopped:
            raise ConnectionError('local bridge is stopped')
        path = self._path()
        async with asyncio.timeout(self.timeout):
            _, writer = await asyncio.open_unix_connection(str(path))
            try:
                self._peer(writer)
            finally:
                writer.close()
                await writer.wait_closed()
        return self.external_session_id

    async def start(self):
        if self._server is not None:
            return
        if self._stopped:
            raise ConnectionError('local bridge is stopped')
        path = self._path()
        self._callback = path.parent / (str(os.getpid())+'-'+uuid4().hex+'.sock')
        try:
            self._server = await asyncio.start_unix_server(self._receive, path=str(self._callback), limit=8192, backlog=16)
            os.chmod(self._callback, 0o600)
        except BaseException:
            await self.aclose()
            raise

    async def _receive(self, reader, writer):
        task = asyncio.current_task()
        if self._stopped or len(self._tasks) >= 16:
            writer.close()
            return
        self._tasks.add(task)
        self._writers.add(writer)
        try:
            self._peer(writer)
            async with asyncio.timeout(self.timeout):
                line = await reader.readline()
                frame = json.loads(line, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            # Callback мог ждать bytes уже после ухода own process из scope.
            # До изменения pending/idle больше нет await.
            if self._stopped:
                return
            self._peer(writer)
            if (type(frame) is not dict or type(frame.get('msgV')) is not int or frame['msgV'] != 1
                    or frame.get('type') != 'control' or type(frame.get('orig_msg_id')) is not str
                    or not 0 < len(frame['orig_msg_id']) <= 256):
                return
            key, action = frame['orig_msg_id'], frame.get('action')
            if action == 'peer_message_status':
                status = frame.get('status')
                if status not in ('delivered', 'denied', 'expired', 'refused', 'dropped'):
                    # held, неизвестный status и отсутствие receipt не доказывают enqueue.
                    return
                keys = [key]
                if status == 'dropped':
                    dropped = frame.get('dropped_msg_ids', [])
                    # Native ограничивает список 256 ID; не расширяем его из wire.
                    if (type(dropped) is not list or len(dropped) > 256
                            or any(type(value) is not str or not 0 < len(value) <= 256 for value in dropped)):
                        return
                    keys.extend(dropped)
                for message_id in keys:
                    future = self._pending.get(message_id)
                    if future is not None and not future.done():
                        # delivered: ранее held input одобрен и released сессии;
                        # это не обычный enqueue ACK и не результат model turn.
                        future.set_result('accepted' if status == 'delivered' else 'unavailable')
            elif action == 'peer_idle_notice' and frame.get('state') in ('idle', 'exited', 'unavailable'):
                for subscription, future in self._idle.values():
                    if key == subscription:
                        if not future.done():
                            future.set_result(frame['state'] == 'idle')
                        break
        except (OSError, ValueError, PermissionError, TimeoutError, RecursionError):
            # Чужой/повреждённый callback не является evidence; ожидатель получит unknown.
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self._writers.discard(writer)
            self._tasks.discard(task)

    async def _write(self, frame, guard=None):
        path = self._path()
        async with asyncio.timeout(self.timeout):
            _, writer = await asyncio.open_unix_connection(str(path))
            self._writers.add(writer)
            try:
                data = []
                if self.own.token is not None:
                    data.append({'type': 'auth', 'token': self.own.token})
                data.append(frame)
                encoded = (''.join(json.dumps(value, ensure_ascii=False)+'\n' for value in data)).encode()
                # Trusted root port, никогда не значение wire/JSON. После всех
                # readiness/connect awaits проверяем живую authority без SQL lock
                # под сетью. От результата guard до write нет другого await.
                if guard is not None:
                    await guard()
                if asyncio.current_task().cancelling():
                    # Cleanup trusted guard не отменяет исходный cancel/deadline.
                    raise asyncio.CancelledError
                if self._stopped:
                    raise ConnectionError('local bridge is stopped')
                # Guard мог уступить event loop: pathname probe до него ничего
                # не говорит о текущем connected peer. Проверка proof и write
                # выполняются синхронно в этом task, без нового yield.
                self._path()
                self._peer(writer)
                writer.write(encoded)
                await writer.drain()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                finally:
                    self._writers.discard(writer)

    async def deliver(self, frame):
        return await self.deliver_guarded(frame, None)

    async def deliver_guarded(self, frame, guard):
        """Root передаёт trusted prewrite port отдельно от пользовательского frame."""
        if self._stopped:
            raise ConnectionError('local bridge is stopped')
        task = asyncio.current_task()
        self._outbound.add(task)
        try:
            return await self._deliver(frame, guard)
        finally:
            self._outbound.discard(task)

    async def _deliver(self, frame, guard):
        if type(frame) is not DeliveryFrame or frame.external_session_id != self.external_session_id:
            raise PermissionError('message targets a different external conversation')
        if not isinstance(frame.text, str) or len(frame.text.encode()) > 65536 or frame.priority not in ('next', 'now'):
            raise ValueError('invalid private delivery frame')
        if self._server is None or self._stopped:
            raise ConnectionError('local bridge is not started')
        digest = hashlib.sha256(json.dumps([str(frame.id), frame.text, frame.priority,
            frame.notify_when_idle, frame.reply_address], ensure_ascii=False).encode()).hexdigest()
        old = self._seen.get(frame.message_id)
        if old:
            if old[0] != digest:
                raise ValueError('local message identity conflicts')
            return ApplicationAcceptance(old[1])
        if len(self._seen) >= 64:
            raise ConnectionError('local bridge delivery capacity exhausted; reconnect without replay')
        # До первого await резервируем ID; неизвестный write не повторяется.
        self._seen[frame.message_id] = (digest, 'unknown')
        future = asyncio.get_running_loop().create_future()
        self._pending[str(frame.message_id)] = future
        callback = 'uds:'+str(self._callback)
        outcome, started = 'unknown', False
        try:
            # Probe доказывает только текущий peer, не application acceptance.
            await self.ready()
            user = {'msgV': 1, 'msg_id': str(frame.message_id), 'type': 'user',
                'session_id': self.external_session_id,
                'message': {'role': 'user', 'content': frame.text}, 'priority': frame.priority, 'from': callback}
            # Private reply address — непрозрачный pool address, не callback path.
            if frame.reply_address:
                user['message']['content'] += '\n\nReply address: '+frame.reply_address
            started = True
            await self._write(user, guard)
            try:
                outcome = await asyncio.wait_for(asyncio.shield(future), self.timeout)
            except TimeoutError:
                outcome = 'unknown'
            if outcome == 'accepted' and frame.notify_when_idle:
                subscription = str(uuid4())
                self._idle[frame.message_id] = (subscription, asyncio.get_running_loop().create_future())
                try:
                    await self._write({'msgV': 1, 'type': 'control', 'msg_id': subscription,
                        'session_id': self.external_session_id,
                        'action': 'notify_when_idle', 'from': callback}, guard)
                except (OSError, TimeoutError, PermissionError, ValueError):
                    # Принятие user-frame уже подтверждено, но idle неизвестен.
                    self._idle.pop(frame.message_id, None)
        except (OSError, TimeoutError):
            outcome = 'unknown' if started else 'unavailable'
        finally:
            self._pending.pop(str(frame.message_id), None)
            if not future.done():
                future.cancel()
            self._seen[frame.message_id] = (digest, outcome)
        return ApplicationAcceptance(outcome)

    async def wait_idle(self, message_id, *, timeout=2):
        if not 0 < timeout <= 5:
            raise ValueError('idle wait must be within 5 seconds')
        pair = self._idle.get(message_id)
        if pair is None:
            return False
        try:
            return await asyncio.wait_for(asyncio.shield(pair[1]), timeout)
        except TimeoutError:
            return False
        finally:
            # Короткий poll timeout не отменяет долгую idle subscription.
            if pair[1].done():
                self._idle.pop(message_id, None)

    async def aclose(self):
        self._stopped = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for writer in tuple(self._writers):
            writer.close()
        tasks = (self._tasks | self._outbound) - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=self.timeout)
            for task in done:
                if not task.cancelled():
                    task.exception()
            if pending:
                # Cancel не доказывает quiescence: authority нельзя отдавать.
                raise RuntimeError('local bridge shutdown incomplete: outbound or callback work')
        if self._callback is not None:
            self._callback.unlink(missing_ok=True)
            self._callback = None
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        for _, future in self._idle.values():
            if not future.done():
                future.cancel()
        self._idle.clear()
