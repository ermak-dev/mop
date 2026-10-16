"""Конечный credential use-case протокол. Только stdlib, без ключей/ORM/crypto RPC."""
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
from uuid import UUID

MAX_FRAME = 2 * 1024 * 1024
MAX_VALUE = 1024 * 1024


class CredentialError(ValueError):
    """Безопасный отказ; transport timeout мутации означает неизвестный исход."""


class Command(str, Enum):
    CREATE = 'create'
    STAGE = 'stage'
    UPDATE = 'update'
    ADDRESSED = 'issue_addressed'
    ROTATION = 'issue_rotation'
    BUNDLE = 'read_bundle'
    PUBLISH_BUNDLE = 'publish_bundle'
    APPROVE_BUNDLE = 'approve_bundle'
    RESERVE_GRANT = 'reserve_grant'
    PROVISION_GRANT = 'provision_grant'
    PUBLISH_PREPARATION = 'publish_preparation'
    READ_PREPARATION = 'read_preparation'
    APPROVE_PREPARATION = 'approve_preparation'


@dataclass(frozen=True, slots=True)
class Credential:
    kind: str
    value: str = field(repr=False)

    def __post_init__(self):
        if type(self.kind) is not str:
            raise CredentialError('current token or session credential required')
        pattern = {'token': r'[0-9a-f]{40}', 'session': r'[a-z0-9]{32}'}.get(self.kind)
        if pattern is None or type(self.value) is not str or not re.fullmatch(pattern, self.value):
            raise CredentialError('current token or session credential required')


@dataclass(frozen=True, slots=True)
class SecretInfo:
    id: UUID
    project_id: UUID | None
    name: str
    kind: str
    purpose: str
    revision: int
    active_version_id: UUID | None
    last_number: int
    revoked: bool
    size: int | None


@dataclass(frozen=True, slots=True)
class VersionInfo:
    id: UUID
    secret_id: UUID
    number: int
    size: int
    key_id: str


@dataclass(frozen=True, slots=True)
class DeliveredSecret:
    binding_id: UUID
    version_id: UUID
    name: str
    kind: str
    value: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class BundleInfo:
    id: UUID
    role: str
    digest: str
    project_id: UUID
    owner_id: int


@dataclass(frozen=True, slots=True)
class BundleBytes:
    id: UUID
    role: str
    digest: str
    manifest: bytes = field(repr=False)
    prompt: bytes = field(repr=False)
    provenance: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PreparationInfo:
    id: UUID
    project_id: UUID
    owner_id: int
    number: int
    content_version_id: UUID
    digest: str
    task_size: int
    vars_size: int


@dataclass(frozen=True, slots=True)
class PreparationBytes:
    tasks: bytes = field(repr=False)
    variables: bytes = field(repr=False)
    provenance: str = field(repr=False)
    commit: str
    secret_refs: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class ApprovalReceipt:
    id: UUID
    revision_id: UUID
    human_id: int


@dataclass(frozen=True, slots=True)
class GrantReceipt:
    id: UUID
    project_id: UUID
    target_id: UUID
    incarnation: UUID
    role: str
    digest: str
    state: str
    revision: int
    credential_version_id: UUID | None


def identifier(value):
    if type(value) is not str:
        raise CredentialError('canonical identifier required')
    try:
        result = UUID(value)
    except ValueError:
        raise CredentialError('canonical identifier required') from None
    if str(result) != value:
        raise CredentialError('canonical identifier required')
    return result


def counter(value):
    if type(value) is not int or not 0 <= value < 2 ** 63:
        raise CredentialError('invalid expected revision or generation')
    return value


def binary(value):
    if type(value) is not str or len(value) > 4 * ((MAX_VALUE + 2) // 3):
        raise CredentialError('invalid credential value size')
    try:
        result = base64.b64decode(value, validate=True)
    except ValueError:
        raise CredentialError('invalid credential value encoding') from None
    if len(result) > MAX_VALUE:
        raise CredentialError('invalid credential value size')
    return result


def encoded(value):
    if type(value) is not bytes or len(value) > MAX_VALUE:
        raise CredentialError('invalid credential value size')
    return base64.b64encode(value).decode('ascii')


def exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise CredentialError('invalid credential command fields')


def _text(value, limit):
    if type(value) is not str or not value or len(value) > limit or '\x00' in value:
        raise CredentialError('invalid credential command text')
    return value


def identifiers(value):
    if type(value) is not list or len(value) > 128:
        raise CredentialError('bounded secret reference list required')
    result = tuple(identifier(item) for item in value)
    if len(set(result)) != len(result):
        raise CredentialError('unique secret references required')
    return result


def instant(value):
    if type(value) is not str or len(value) > 40:
        raise CredentialError('canonical UTC grant expiry required')
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise CredentialError('canonical UTC grant expiry required') from None
    if result.utcoffset() != timezone.utc.utcoffset(result) or result.isoformat() != value:
        raise CredentialError('canonical UTC grant expiry required')
    return result


def parse_request(data):
    exact(data, ('version', 'auth', 'command', 'args'))
    if type(data['version']) is not int or data['version'] != 1:
        raise CredentialError('unsupported credential protocol version')
    exact(data['auth'], ('kind', 'value'))
    auth = Credential(**data['auth'])
    try:
        command = Command(data['command'])
    except (TypeError, ValueError):
        raise CredentialError('unsupported credential command') from None
    args = data['args']
    shapes = {Command.CREATE: ('project_id', 'name', 'kind', 'purpose', 'value'),
              Command.STAGE: ('secret_id', 'value', 'expected_revision'),
              Command.UPDATE: ('secret_id', 'value', 'expected_revision'),
              Command.ADDRESSED: ('target_id',), Command.ROTATION: ('rotation_id', 'claim_id', 'generation'),
              Command.BUNDLE: ('bundle_id',),
              Command.PUBLISH_BUNDLE: ('project_id', 'manifest', 'prompt', 'provenance'),
              Command.APPROVE_BUNDLE: ('bundle_id',),
              Command.RESERVE_GRANT: ('project_id', 'bundle_id', 'target_id', 'incarnation', 'request_id', 'expires_at'),
              Command.PROVISION_GRANT: ('grant_id', 'expected_revision'),
              Command.PUBLISH_PREPARATION: ('project_id', 'tasks', 'variables', 'provenance', 'commit', 'secret_refs'),
              Command.READ_PREPARATION: ('revision_id',), Command.APPROVE_PREPARATION: ('revision_id',)}
    exact(args, shapes[command])
    result = dict(args)
    for name in ('project_id', 'secret_id', 'rotation_id', 'claim_id', 'bundle_id',
                 'grant_id', 'revision_id', 'target_id', 'incarnation', 'request_id'):
        if name in result:
            optional = command is Command.CREATE and name == 'project_id' and result[name] is None
            result[name] = None if optional else identifier(result[name])
    for name in ('expected_revision', 'generation'):
        if name in result:
            result[name] = counter(result[name])
    for name, limit in (('name', 256), ('kind', 32), ('purpose', 32), ('provenance', 4096), ('commit', 64)):
        if name in result:
            result[name] = _text(result[name], limit)
    for name in ('value', 'manifest', 'prompt', 'tasks', 'variables'):
        if name in result:
            result[name] = binary(result[name])
    if 'secret_refs' in result:
        result['secret_refs'] = identifiers(result['secret_refs'])
    if 'expires_at' in result:
        result['expires_at'] = instant(result['expires_at'])
    return auth, command, result


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CredentialError('duplicate credential protocol field')
        result[key] = value
    return result


def loads(data):
    try:
        return json.loads(data, object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError):
        raise CredentialError('invalid credential protocol frame') from None


def frame(data):
    raw = json.dumps(data, separators=(',', ':'), ensure_ascii=True).encode('ascii')
    if len(raw) > MAX_FRAME:
        raise CredentialError('credential protocol frame exceeds size limit')
    return struct.pack('!I', len(raw)) + raw


def frame_size(data):
    size = struct.unpack('!I', data)[0]
    if not 1 <= size <= MAX_FRAME:
        raise CredentialError('credential protocol frame exceeds size limit')
    return size


def socket_path(value):
    if type(value) is not str or not value or '\x00' in value or len(os.fsencode(value)) > 100:
        raise CredentialError('explicit absolute credential socket path required')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise CredentialError('explicit absolute credential socket path required')
    return path


def directory_fd(path, owner):
    """Закреплённый dirfd: symlink/чужой writer не может заменить endpoint."""
    fd = None
    try:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parent.parts[1:]:
            info = os.fstat(fd)
            if info.st_uid not in {0, owner} or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
                raise CredentialError('credential socket directory has untrusted replacement access')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != owner or info.st_mode & 0o022:
            raise CredentialError('credential socket parent must be service-owned without other writers')
        result, fd = fd, None
        return result
    except OSError:
        raise CredentialError('protected credential socket directory is unavailable') from None
    finally:
        if fd is not None:
            os.close(fd)


def peer_uid(sock):
    if not hasattr(socket, 'SO_PEERCRED'):
        raise CredentialError('credential socket requires SO_PEERCRED')
    return struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))[1]
