"""Read-only mop observer физической native сессии (#643).

Native CLI публикует registry/key/socket, не mop scope authority. Producer
принадлежит явно подключённому lifecycle owner. Observer работает внутри тела
в namespace producer; ни retirement, ни model readiness он не подтверждает.
Проверки до/после await не являются syscall-atomic fencing будущего ввода.
"""
import asyncio
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Protocol
from uuid import UUID

from mop import session
from mop.driver.launch import RegisteredLayout
from mop.node.execution_adapters import SessionProof, process_identity
from mop.node.http_execution import Refused, Unavailable


@dataclass(frozen=True)
class NativeIdentity:
    """Факты OS/native, без токена, transcript и утверждений о полномочиях."""
    pane: str
    pid: int
    process_start: str
    session_id: str
    config: Path
    socket_inode: tuple
    tmux_identity: tuple
    tty_identity: tuple
    namespaces: tuple


class ProducerOwner(Protocol):
    async def producer_epoch(self, layout: RegisteredLayout, identity: NativeIdentity) -> UUID | None:
        """Только trusted lifecycle owner: epoch стабилен пока producer attached.

        Lookup существующего attachment, не mint на каждом observe. Владелец
        проверяет incarnation/executor и native identity; hooks, executor_epoch
        и registry не являются источниками producer authority. None -- отказ.
        Attached owner также может дать синхронный producer_changed(layout),
        чтобы потеря physical evidence навсегда закрыла именно этот source slot.
        """
        ...


def _socket_stat(path):
    # Без symlink во всём пути, включая промежуточный runtime directory.
    fd = session.identity_open(str(path.parent), directory=True)
    try:
        value = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
    finally:
        os.close(fd)
    if not stat.S_ISSOCK(value.st_mode) or value.st_uid != os.getuid():
        raise Unavailable('native physical socket identity unavailable')
    return value.st_dev, value.st_ino


def _pane(layout):
    result = subprocess.run(('tmux', '-S', str(layout.tmux_socket), 'list-panes', '-a',
        '-F', '#{pane_id}\t#{pane_tty}\t#{pane_pid}\t#{pane_dead}'),
        capture_output=True, text=True, check=True, timeout=1)
    lines = result.stdout.strip().splitlines()
    if len(lines) != 1:
        raise Unavailable('native registered pane is ambiguous or unavailable')
    fields = lines[0].split('\t')
    if (len(fields) != 4 or not re.fullmatch(r'%[0-9]+', fields[0])
            or not re.fullmatch(r'/dev/pts/[0-9]+', fields[1])
            or not re.fullmatch(r'[1-9][0-9]*', fields[2]) or fields[3] != '0'):
        raise Unavailable('native registered pane identity unavailable')
    return fields[0], fields[1], int(fields[2])


def _json_file(config, parts, limit, *, private=False):
    fd = session.identity_open(str(config), parts)
    with os.fdopen(fd, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if metadata.st_uid != os.getuid() or (private and metadata.st_mode & 0o077):
            raise Unavailable('native registry or key ownership unavailable')
        raw = stream.read(limit+1)
    if len(raw) > limit:
        raise ValueError('native identity JSON is too large')
    return session.identity_json(raw)


def _registry(config, pid, clone, path):
    fd = session.identity_open(str(config), ('sessions',), directory=True)
    try:
        names = sorted(os.listdir(fd))
    finally:
        os.close(fd)
    if len(names) > 1024:
        raise Unavailable('native registry exceeds bounded observation')
    hits = []
    for name in names:
        if not name.endswith('.json'):
            continue
        try:
            record = _json_file(config, ('sessions', name), 16*1024)
        except ValueError:
            continue  # Неполные чужие registry записи -- не physical evidence.
        # Не пробуем ни одного endpoint, названного чужой записью, даже в cwd.
        if record.get('messagingSocketPath') != str(path):
            continue
        if (type(record.get('pid')) is not int or record['pid'] != pid
                or record.get('cwd') != str(clone)
                or type(record.get('sessionId')) is not str
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', record['sessionId'])):
            raise Unavailable('native registry differs from registered physical session')
        hits.append(record['sessionId'])
    if len(hits) != 1:
        raise Unavailable('native registry is ambiguous or unavailable')
    return hits[0]


def _process(pid, layout, tty):
    value = session.native_process(pid)
    own_namespaces = tuple((kind, os.stat(f'/proc/self/ns/{kind}').st_ino)
                           for kind in ('mnt', 'pid', 'user'))
    device = os.stat(tty)
    if (value['uid'] != os.getuid() or value['cwd'] != str(layout.clone)
            or value['namespaces'] != own_namespaces
            or value['start'] != process_identity(pid)
            or value['pgrp'] != value['foreground'] or value['foreground'] <= 0
            or not stat.S_ISCHR(device.st_mode) or not value['stdin_character']
            or not value['tty'] or device.st_rdev != value['tty']
            or device.st_rdev != value['stdin_device']):
        raise Unavailable('native process namespace, clone, start or foreground TTY changed')
    config = Path(session.process_config(value['env']))
    return value, config, (tty, device.st_dev, device.st_ino, device.st_rdev,
                           value['pgrp'], value['foreground'])


def _snapshot(layout):
    tmux_inode = _socket_stat(layout.tmux_socket)
    tmux_peer = session.socket_peer(str(layout.tmux_socket))
    if tmux_peer is None or tmux_peer[1] != os.getuid() or tuple(tmux_peer[2]) != tmux_inode:
        raise Unavailable('registered tmux socket peer unavailable')
    tmux_start = process_identity(tmux_peer[0])
    pane, tty, pid = pane_before = _pane(layout)
    before, config, tty_identity = _process(pid, layout, tty)
    # Clone тоже не может стать ссылкой в соседнюю регистрацию.
    fd = session.identity_open(str(layout.clone), directory=True)
    os.close(fd)
    path = layout.runtime/'session.sock'
    sid = _registry(config, pid, layout.clone, path)
    socket_inode = _socket_stat(path)
    peer = session.socket_peer(str(path))
    if peer != (pid, os.getuid(), list(socket_inode)):
        raise Unavailable('native session socket peer differs from pane process')
    key_name = f'{pid}.'+hashlib.sha256(str(path).encode()).hexdigest()+'.key'
    key = _json_file(config, ('sessions', key_name), 4096, private=True)
    # procStart документирован как строка ticks. Не выдумываем семантику
    # pidDomain: локальные PID/mnt/user namespaces доказаны через /proc.
    if (type(key.get('procStart')) is not str or key['procStart'] != before['proc_start']
            or not re.fullmatch(r'[0-9]{1,32}', key['procStart'])):
        raise Unavailable('native process-start key is unknown or stale')
    after, final_config, final_tty = _process(pid, layout, tty)
    if (before != after or config != final_config or tty_identity != final_tty
            or _pane(layout) != pane_before or _socket_stat(path) != socket_inode
            or _socket_stat(layout.tmux_socket) != tmux_inode
            or session.socket_peer(str(layout.tmux_socket)) != tmux_peer
            or process_identity(tmux_peer[0]) != tmux_start
            or _registry(config, pid, layout.clone, path) != sid
            or _json_file(config, ('sessions', key_name), 4096, private=True).get('procStart') != key['procStart']):
        raise Unavailable('native physical session changed during observation')
    return NativeIdentity(pane, pid, before['start'], sid, config, socket_inode,
        (tmux_inode, tmux_peer[0], tmux_start), tty_identity, before['namespaces'])


class NativePhysicalSessions:
    def __init__(self, *, owner: ProducerOwner | None = None):
        self.owner = owner

    def _changed(self, layout):
        # Attached owner может без await закрыть source slot. Даже если старый
        # inode/registry вернётся к следующему observe, producer не воскреснет.
        changed = getattr(self.owner, 'producer_changed', None)
        if callable(changed):
            changed(layout)

    async def _snapshot(self, layout):
        try:
            return await asyncio.to_thread(_snapshot, layout)
        except (Unavailable, OSError, ValueError, LookupError, subprocess.SubprocessError):
            self._changed(layout)
            raise

    async def observe(self, layout):
        if type(layout) is not RegisteredLayout:
            raise Refused('trusted registered native layout required')
        if self.owner is None or not callable(getattr(self.owner, 'producer_epoch', None)):
            raise Unavailable('native producer lifecycle owner is not bound')
        try:
            async with asyncio.timeout(5):
                identity = await self._snapshot(layout)
                epoch = await self.owner.producer_epoch(layout, identity)
                if type(epoch) is not UUID or epoch == UUID(layout.registration.work.executor_epoch):
                    raise Unavailable('native producer lifecycle owner has no independent attached epoch')
                if await self._snapshot(layout) != identity:
                    self._changed(layout)
                    raise Unavailable('native physical session changed during owner lookup')
                if await self.owner.producer_epoch(layout, identity) != epoch:
                    self._changed(layout)
                    raise Unavailable('native producer lifecycle owner epoch changed')
                if await self._snapshot(layout) != identity:
                    self._changed(layout)
                    raise Unavailable('native physical session changed during owner confirmation')
                # Последний to_thread тоже await: за это время root мог закрыть
                # source. Direct observe обязан соблюдать тот же lifetime gate,
                # что и view.proof, даже при неизменном физическом evidence.
                if await self.owner.producer_epoch(layout, identity) != epoch:
                    self._changed(layout)
                    raise Unavailable('native producer lifecycle owner closed during final observation')
            return SessionProof(layout.registration, identity.pane, identity.session_id, epoch,
                                layout.runtime/'session.sock', identity.pid, identity.process_start)
        except asyncio.CancelledError:
            # to_thread продолжает жить после отмены await. Его поздний успех
            # или ошибка уже не могут разрешить прежний source/единственный ACK.
            # Инвалидируем синхронно, прежде чем отдавать отмену caller.
            self._changed(layout)
            raise
        except TimeoutError as error:
            self._changed(layout)
            raise Unavailable('native physical session evidence unavailable') from error
        except Refused:
            raise  # Без повторной санитизации безопасной причины lifecycle owner.
        except (OSError, ValueError, LookupError, subprocess.SubprocessError) as error:
            raise Unavailable('native physical session evidence unavailable') from error

    async def reconcile(self, node_id, previous_epoch, new_epoch):
        # Отсутствие pane/PID не доказывает конец всех descendants/effects.
        raise Unavailable('physical scope retirement reconciliation is not bound')
