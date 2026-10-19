"""Конечная process-owned HOST authority; не permissions или policy snapshot.

BackendState — недоверенные metadata до проверки живого root Unix peer.
Nonce создаёт сам supervisor; node не может передать его через bind.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re
from uuid import UUID

from mop.node.host_scope import Refused, canonical, decode, encode, exact


def nonzero(value):
    value = canonical(value)
    if UUID(value).int == 0:
        raise Refused('nonzero HOST authority UUID required')
    return value


@dataclass(frozen=True, slots=True)
class Identity:
    installation_id: str
    node_id: str

    def __post_init__(self):
        if (type(self.installation_id) is not str or
                not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}', self.installation_id)):
            raise Refused('immutable installed HOST installation identity required')
        nonzero(self.node_id)

    def wire(self):
        return dict(installation_id=self.installation_id, node_id=self.node_id)

    @classmethod
    def parse(cls, value):
        exact(value, ('installation_id', 'node_id'))
        return cls(**value)


@dataclass(frozen=True, slots=True)
class Process:
    pid: int
    start: int
    boot_id: str

    def __post_init__(self):
        if (type(self.pid) is not int or not 0 < self.pid < 2**31 or
                type(self.start) is not int or not 0 < self.start < 2**64):
            raise Refused('kernel PID/starttime required for HOST authority')
        nonzero(self.boot_id)

    @classmethod
    def read(cls, pid):
        from mop.node.host_scope_systemd import open_directory, parse_proc_start, read_bytes_at, read_kernel
        fd = open_directory(Path('/proc')/str(pid))
        try:
            start = parse_proc_start(read_bytes_at(fd, 'stat'), pid)
            return cls(pid, start, read_kernel('/proc/sys/kernel/random/boot_id', 64))
        finally:
            os.close(fd)

    @classmethod
    def current(cls):
        return cls.read(os.getpid())

    def wire(self):
        return dict(pid=self.pid, start=self.start, boot_id=self.boot_id)

    @classmethod
    def parse(cls, value):
        exact(value, ('pid', 'start', 'boot_id'))
        return cls(**value)


@dataclass(frozen=True, slots=True)
class BackendRequest:
    action: str
    request_id: str
    identity: Identity
    epoch: str | None = None
    executor_epoch: str | None = None
    empty: bool | None = None

    def __post_init__(self):
        nonzero(self.request_id)
        if type(self.identity) is not Identity or self.action not in (
                'backend_query', 'backend_bind', 'backend_confirm'):
            raise Refused('finite HOST backend action required')
        if self.action == 'backend_query':
            if any(v is not None for v in (self.epoch, self.executor_epoch, self.empty)):
                raise Refused('HOST query carries no binding authority')
        else:
            nonzero(self.epoch)
            nonzero(self.executor_epoch)
            if type(self.empty) is not bool:
                raise Refused('live SQL empty observation required for HOST binding')

    def wire(self):
        return dict(schema_version=1, action=self.action, request_id=self.request_id,
            identity=self.identity.wire(), epoch=self.epoch,
            executor_epoch=self.executor_epoch, empty=self.empty)

    def encode(self):
        return encode(self.wire())

    @classmethod
    def decode(cls, raw):
        value = decode(raw)
        exact(value, ('schema_version', 'action', 'request_id', 'identity', 'epoch', 'executor_epoch', 'empty'))
        if type(value['schema_version']) is not int or value['schema_version'] != 1:
            raise Refused('unsupported HOST backend schema')
        return cls(value['action'], value['request_id'], Identity.parse(value['identity']),
                   value['epoch'], value['executor_epoch'], value['empty'])


@dataclass(frozen=True, slots=True)
class BackendState:
    identity: Identity
    executor_epoch: str
    bound_epoch: str | None
    process: Process
    mode: str

    def __post_init__(self):
        if type(self.identity) is not Identity or type(self.process) is not Process:
            raise Refused('typed HOST backend process/identity required')
        nonzero(self.executor_epoch)
        if self.bound_epoch is not None:
            nonzero(self.bound_epoch)
        if type(self.mode) is not str or self.mode not in ('query', 'first', 'resume'):
            raise Refused('finite HOST backend mode required')
        if self.mode != 'query' and self.bound_epoch is None:
            raise Refused('HOST backend is not bound')

    def wire(self):
        return dict(identity=self.identity.wire(), executor_epoch=self.executor_epoch,
                    bound_epoch=self.bound_epoch, process=self.process.wire(), mode=self.mode)

    @classmethod
    def parse(cls, value):
        exact(value, ('identity', 'executor_epoch', 'bound_epoch', 'process', 'mode'))
        return cls(Identity.parse(value['identity']), value['executor_epoch'], value['bound_epoch'],
                   Process.parse(value['process']), value['mode'])


_VERIFIED = object()


class VerifiedAuthority:
    """Только живое соединение проверенного root peer выдаёт этот локальный proof."""
    __slots__ = ('state', '_session')

    def __init__(self, state, session=None, *, _proof=None):
        if _proof is not _VERIFIED or type(state) is not BackendState or session is None:
            raise Refused('live verified root HOST backend authority required')
        self.state, self._session = state, session

    def check(self):
        self._session.check(self)
        return self.state
