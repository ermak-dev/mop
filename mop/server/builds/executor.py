"""Unix protocol: отдельная OS identity и finite durable claim, не node principal.

Socket path/uid/ports/resolver задаёт trusted Root. Request не содержит argv,
ключей, inventory, owner или выбираемых классов. Разрыв клиента не отменяет effect.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import socket
import stat
import struct
from uuid import UUID

from .contracts import StageResult


@dataclass(frozen=True)
class ExecutorRequest:
    intent_id: UUID
    step_id: UUID
    token: UUID
    epoch: UUID
    verb: str


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate executor field')
        value[key] = item
    return value


def decode_request(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= 2048:
        raise ValueError('executor request exceeds protocol bound')
    try:
        value = json.loads(raw, object_pairs_hook=_object)
        if (type(value) is not dict or set(value) != {'version','intent_id','step_id','token','epoch','verb'}
                or type(value['version']) is not int or value['version'] != 1
                or value['verb'] not in ('apply','inspect')):
            raise ValueError()
        return ExecutorRequest(*(UUID(value[key]) for key in ('intent_id','step_id','token','epoch')), value['verb'])
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError('invalid scoped executor request') from None


def _peer(connection):
    if not hasattr(socket, 'SO_PEERCRED'):
        raise RuntimeError('Unix peer credentials are required')
    pid, uid, gid = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    return uid


def _read(connection, maximum):
    data = bytearray()
    while len(data) <= maximum:
        part = connection.recv(min(512, maximum+1-len(data)))
        if not part:
            raise ValueError('incomplete executor frame')
        data.extend(part)
        if b'\n' in part:
            frame, extra = bytes(data).split(b'\n', 1)
            if extra:
                raise ValueError('only one executor frame is allowed')
            return frame
    raise ValueError('executor frame exceeds bound')


class UnixPrivilegedClient:
    capabilities = frozenset({'node_executor','inspect'})

    def __init__(self, *, path, executor_uid, timeout=7205, availability_reader=None):
        if executor_uid in (0, os.getuid()) or not 1 <= timeout <= 7205:
            raise ValueError('privileged executor requires a distinct non-root OS identity')
        self.path, self.uid, self.timeout = str(path), executor_uid, timeout
        self.availability_reader = availability_reader
        self.capabilities = self.capabilities | (frozenset({'image_builder'}) if availability_reader is not None else frozenset())

    def availability(self, build, node_id):
        if self.availability_reader is None:
            raise RuntimeError('fresh physical image availability query is not bound')
        return self.availability_reader(build, node_id)

    def _call(self, ticket, verb):
        request = dict(version=1, intent_id=str(ticket.id), step_id=str(ticket.claim.step_id),
            token=str(ticket.claim.token), epoch=str(ticket.claim.epoch), verb=verb)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            connection.connect(self.path)
            if _peer(connection) != self.uid:
                raise PermissionError('privileged executor OS identity changed')
            connection.sendall(json.dumps(request, separators=(',', ':')).encode()+b'\n')
            value = json.loads(_read(connection, 2048), object_pairs_hook=_object)
        if type(value) is not dict or set(value) != {'outcome','quiescent','reason'}:
            raise ValueError('invalid executor response')
        return StageResult(**value)

    def apply(self, ticket):
        return self._call(ticket, 'apply')

    def inspect(self, ticket):
        return self._call(ticket, 'inspect')


class PrivilegedExecutor:
    """Один listener/process, без своей очереди и журналов. Root gate — SQL callback.

    resolve фиксирует единственный dispatch до возврата ticket. record сохраняет
    result даже после disconnect caller. Ошибка БД оставляет общий Step unknown.
    """
    def __init__(self, *, path, caller_uid, resolve, record, image_builder, node_executor, request_timeout=5):
        if os.getuid() == 0 or caller_uid == os.getuid() or not 0 < request_timeout <= 5:
            raise ValueError('privileged executor must run under a distinct non-root OS identity')
        if (not {'image_builder','inspect'} <= getattr(image_builder, 'capabilities', frozenset())
                or not {'node_executor','inspect'} <= getattr(node_executor, 'capabilities', frozenset())):
            raise ValueError('privileged executor capabilities are not bound')
        self.path, self.caller_uid = Path(path), caller_uid
        self.resolve, self.record = resolve, record
        self.image_builder, self.node_executor = image_builder, node_executor
        self.timeout, self.listener = request_timeout, None

    def open(self):
        directory = self.path.parent.lstat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid() or directory.st_mode & 0o007:
            raise ValueError('executor socket requires its own private directory')
        if self.path.exists() or self.path.is_symlink():
            raise ValueError('executor socket path already exists; authorized cleanup required')
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        os.chmod(self.path, 0o660)
        self.listener.listen(4)

    def serve_one(self):
        if self.listener is None:
            raise RuntimeError('executor listener is not open')
        connection, _ = self.listener.accept()
        with connection:
            connection.settimeout(self.timeout)
            try:
                peer_uid = _peer(connection)
                # Один bounded frame дочитывается и при refusal: закрытие Unix
                # socket с непрочитанным input иначе превращает verdict в reset.
                raw = _read(connection, 2048)
                if peer_uid != self.caller_uid:
                    raise PermissionError('executor peer is outside enrollment')
                request = decode_request(raw)
                ticket = self.resolve(intent_id=request.intent_id, step_id=request.step_id,
                    token=request.token, epoch=request.epoch, verb=request.verb)
                if (ticket.id != request.intent_id or ticket.claim.step_id != request.step_id
                        or ticket.claim.token != request.token or ticket.claim.epoch != request.epoch):
                    raise PermissionError('executor resolver returned a different claim')
                if ticket.stage not in ('build','wipe'):
                    raise PermissionError('executor stage is outside privileged enrollment')
                port = self.image_builder if ticket.stage == 'build' else self.node_executor
                result = port.apply(ticket) if request.verb == 'apply' else port.inspect(ticket)
                if type(result) is not StageResult:
                    raise ValueError('executor requires typed evidence')
                self.record(ticket.claim, ticket, result)
            except Exception as error:
                result = StageResult('unknown', False, 'privileged executor refused: '+type(error).__name__)
            try:
                connection.sendall(json.dumps(dict(outcome=result.outcome, quiescent=result.quiescent, reason=result.reason)).encode()+b'\n')
            except (BrokenPipeError, ConnectionResetError):
                # Result уже записан; новый caller получит его через общий REST.
                pass

    def close(self):
        if self.listener is not None:
            self.listener.close()
            self.listener = None
            self.path.unlink()
