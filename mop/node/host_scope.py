"""Конечный OS-контракт HOST #719, не business registry и не readiness.

Scope — адрес, не полномочие. Supervisor доверяет установленному node UID;
текущий server admission проверяет node client до обращения к сокету.
"""
import base64
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Protocol
from uuid import UUID

from mop.node.http_execution import Refused

FRAME_LIMIT = 4096


class Unknown(RuntimeError):
    """Эффект/наблюдение неизвестны; отсутствие ACK не разрешает повтор."""


def canonical(value):
    try:
        if type(value) is str and str(UUID(value)) == value:
            return value
    except ValueError:
        pass
    raise Refused('canonical UUID required')


def invocation(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{32}', value) or value == '0'*32:
        raise Refused('actual nonzero InvocationID required')
    return value


def exact(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise Refused('finite HOST schema required')


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused('duplicate HOST JSON field')
        result[key] = value
    return result


def decode(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= FRAME_LIMIT:
        raise Refused('bounded HOST frame required')
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise Refused('invalid HOST JSON') from error


def encode(value):
    raw = json.dumps(value, separators=(',', ':'), ensure_ascii=True).encode()
    if len(raw) > FRAME_LIMIT:
        raise Refused('HOST frame exceeds limit')
    return raw


@dataclass(frozen=True)
class Scope:
    epoch: str
    node_id: str
    executor_epoch: str
    target_id: str
    incarnation_id: str
    launch_id: str

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            canonical(getattr(self, name))

    def wire(self):
        return dict(self.__dict__)

    @classmethod
    def parse(cls, value):
        exact(value, cls.__dataclass_fields__)
        return cls(**value)

    @property
    def unit(self):
        # Hex шести UUID переполняет NAME_MAX у systemd PrivateTmp. Полное
        # reversible base32 encoding короче, без hash collision/sidecar lookup.
        raw = b''.join(UUID(v).bytes for v in self.__dict__.values())
        return 'mop-host-' + base64.b32encode(raw).decode().rstrip('=').lower() + '.service'

    @property
    def cgroup(self):
        return '/mop.slice/mop-host.slice/' + self.unit

    @classmethod
    def from_unit(cls, value):
        if type(value) is not str or not re.fullmatch(r'mop-host-[a-z2-7]{154}\.service', value):
            raise Refused('UUID-derived HOST unit required')
        raw = base64.b32decode(value[9:-8].upper()+'======')
        result = cls(*(str(UUID(bytes=raw[i:i+16])) for i in range(0, 96, 16)))
        if result.unit != value:
            raise Refused('noncanonical HOST unit')
        return result


@dataclass(frozen=True)
class Installed:
    """Только root-installed descriptor; никогда не десериализуется с socket."""
    node_uid: int
    body_uid: int
    body_gid: int
    runner: Path
    body_root: Path
    node_private: Path

    def __post_init__(self):
        for value in (self.node_uid, self.body_uid, self.body_gid):
            if type(value) is not int or not 0 < value < 2**31:
                raise Refused('dedicated non-root installed UID/GID required')
        if self.node_uid == self.body_uid:
            raise Refused('node-service and body UID must differ')
        for path in (self.runner, self.body_root, self.node_private):
            if (not isinstance(path, Path) or not path.is_absolute() or '..' in path.parts
                    or path == Path('/') or not re.fullmatch(r'/[a-zA-Z0-9_./-]{1,180}', str(path))):
                raise Refused('fixed installed absolute path required')
        paths = (self.runner, self.body_root, self.node_private)
        if any(a == b or a in b.parents or b in a.parents
               for i, a in enumerate(paths) for b in paths[i+1:]):
            raise Refused('runner, body and node-private trees must be separate')

    def body(self, scope):
        return self.body_root / scope.launch_id


@dataclass(frozen=True)
class Reference:
    scope: Scope
    invocation_id: str
    boot_id: str

    def __post_init__(self):
        if type(self.scope) is not Scope:
            raise Refused('typed HOST scope required')
        invocation(self.invocation_id)
        canonical(self.boot_id)

    def wire(self):
        return dict(scope=self.scope.wire(), invocation_id=self.invocation_id, boot_id=self.boot_id)

    @classmethod
    def parse(cls, value):
        exact(value, ('scope', 'invocation_id', 'boot_id'))
        return cls(Scope.parse(value['scope']), value['invocation_id'], value['boot_id'])


@dataclass(frozen=True)
class Request:
    action: str
    scope: Scope
    request_id: str
    reference: Reference | None = None

    def __post_init__(self):
        canonical(self.request_id)
        if type(self.scope) is not Scope or self.action not in ('start', 'inspect', 'retire'):
            raise Refused('finite HOST action required')
        if self.action == 'start':
            if self.reference is not None:
                raise Refused('start cannot replay an existing reference')
        elif type(self.reference) is not Reference or self.reference.scope != self.scope:
            raise Refused('exact HOST reference required')

    def wire(self):
        return dict(schema_version=1, action=self.action, scope=self.scope.wire(),
                    request_id=self.request_id, reference=self.reference.wire() if self.reference else None)

    def encode(self):
        return encode(self.wire())

    @classmethod
    def decode(cls, raw):
        value = decode(raw)
        exact(value, ('schema_version', 'action', 'scope', 'request_id', 'reference'))
        if type(value['schema_version']) is not int or value['schema_version'] != 1:
            raise Refused('unsupported HOST schema')
        ref = Reference.parse(value['reference']) if value['reference'] is not None else None
        return cls(value['action'], Scope.parse(value['scope']), value['request_id'], ref)


@dataclass(frozen=True)
class Snapshot:
    reference: Reference
    populated: bool

    def __post_init__(self):
        if type(self.reference) is not Reference or type(self.populated) is not bool:
            raise Refused('typed physical HOST snapshot required')

    def wire(self):
        return dict(reference=self.reference.wire(), populated=self.populated)

    @classmethod
    def parse(cls, value):
        exact(value, ('reference', 'populated'))
        return cls(Reference.parse(value['reference']), value['populated'])


@dataclass(frozen=True)
class Facts:
    """Внутренний OS port: поля получены от manager/kernel, не из запроса."""
    scope: Scope
    invocation_id: str
    boot_id: str
    uid: int
    gid: int
    executable: str
    argv: tuple[str, ...]
    cgroup: str
    populated: bool
    nondelegated: bool

    def verify(self, installed, scope, reference=None):
        current = Reference(self.scope, self.invocation_id, self.boot_id)
        if (self.scope != scope or self.uid != installed.body_uid or self.gid != installed.body_gid
                or self.executable != str(installed.runner)
                or self.argv != (str(installed.runner), '--scope', scope.unit)
                or self.cgroup != scope.cgroup or self.nondelegated is not True
                or type(self.populated) is not bool
                or reference is not None and current != reference):
            raise Refused('HOST unit ownership or actual OS facts mismatch')
        return Snapshot(current, self.populated)


class HostScopes(Protocol):
    async def start(self, scope: Scope, guard) -> Snapshot: ...
    async def inspect(self, reference: Reference, guard) -> Snapshot: ...
    async def retire(self, reference: Reference, guard) -> Snapshot: ...


class OsAdapter(Protocol):
    async def inspect(self, scope: Scope) -> Facts | None: ...
    async def start(self, scope: Scope) -> Facts: ...
    async def retire(self, reference: Reference) -> Facts: ...
