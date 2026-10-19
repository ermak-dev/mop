"""Private transport input #722; не локальная authority или discovery store."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import ssl
import stat
from urllib.parse import urlsplit
from uuid import UUID

MAX_BOOTSTRAP_BYTES = 16384
MAX_CA_BYTES = 262144
FIELDS = frozenset(('schema_version', 'installation_id', 'origin', 'ca_file', 'node_id', 'token'))


class BootstrapError(ValueError):
    """Безопасный отказ без отражения private bytes/путей из входа."""


def _path(value):
    if not isinstance(value, (str, os.PathLike)):
        raise BootstrapError('node bootstrap requires canonical absolute file paths')
    raw = os.fspath(value)
    if (type(raw) is not str or not raw.startswith('/') or raw == '/'
            or any(ord(c) < 32 or ord(c) == 127 or 0xd800 <= ord(c) <= 0xdfff for c in raw)
            or any(part in ('', '.', '..') for part in raw.split('/')[1:])):
        raise BootstrapError('node bootstrap requires canonical absolute file paths')
    return Path(raw)


def _read_private(path, limit):
    """Dirfd/no-follow на каждом компоненте; FIFO не успевает заблокировать open."""
    directory = descriptor = None
    try:
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        for name in path.parts[1:-1]:
            ancestor = os.fstat(directory)
            if (ancestor.st_uid not in (0, os.geteuid())
                    or ancestor.st_mode & 0o022 and not ancestor.st_mode & stat.S_ISVTX):
                raise BootstrapError('node bootstrap requires trusted directory ancestors')
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        parent = os.fstat(directory)
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise BootstrapError('node bootstrap requires an owned private directory')
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=directory)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) not in (0o400, 0o600) or before.st_nlink != 1
                or not 0 < before.st_size <= limit):
            raise BootstrapError('node bootstrap requires owned private bounded regular files')
        chunks, remaining = [], limit + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        data = b''.join(chunks)
        stable = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink',
                  'st_size', 'st_mtime_ns', 'st_ctime_ns')
        # Kernel atime может измениться от нашего read; это не смена input.
        if (len(data) > limit or any(getattr(before, key) != getattr(after, key) for key in stable)
                or len(data) != after.st_size):
            raise BootstrapError('node bootstrap input changed or exceeded its bound')
        return data
    except OSError:
        raise BootstrapError('node bootstrap private input is unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def _unique(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise BootstrapError('duplicate node bootstrap field')
        value[name] = item
    return value


def _nonfinite(value):
    raise BootstrapError('finite node bootstrap JSON required')


def _origin(value):
    if (type(value) is not str or not value.isascii()
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        raise BootstrapError('node bootstrap requires a canonical HTTPS origin')
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == 'https' and parts.hostname and parts.username is None
            and parts.password is None and not parts.path and not parts.query and not parts.fragment
            and parts.port != 0 and '?' not in value and '#' not in value
            and not parts.netloc.endswith(':') and '\\' not in value)
    except ValueError:
        valid = False
    if not valid:
        raise BootstrapError('node bootstrap requires a canonical HTTPS origin')
    return value


@dataclass(frozen=True, slots=True)
class BootstrapTransport:
    installation_id: str
    origin: str
    ca_file: Path
    node_id: str
    token: str = field(repr=False)
    _ca: bytes = field(repr=False)

    def tls(self):
        """Только прочитанный no-follow CA; без повторного открытия и system trust."""
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cadata=self._ca.decode('ascii'))
            return context
        except (ValueError, UnicodeError, ssl.SSLError):
            raise BootstrapError('node bootstrap CA is invalid') from None


def load(file):
    path = _path(file)
    try:
        value = json.loads(_read_private(path, MAX_BOOTSTRAP_BYTES).decode('utf-8'),
                           object_pairs_hook=_unique, parse_constant=_nonfinite)
    except BootstrapError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise BootstrapError('invalid finite node bootstrap JSON') from None
    if (type(value) is not dict or set(value) != FIELDS
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        raise BootstrapError('unsupported node bootstrap schema or fields')
    identity, token, node_id = value['installation_id'], value['token'], value['node_id']
    if type(identity) is not str or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}', identity):
        raise BootstrapError('node bootstrap installation identity is invalid')
    try:
        if type(node_id) is not str or str(UUID(node_id)) != node_id or UUID(node_id).int == 0:
            raise ValueError
    except ValueError:
        raise BootstrapError('node bootstrap requires a canonical node UUID') from None
    if type(token) is not str or not re.fullmatch('[0-9a-f]{40}', token):
        raise BootstrapError('node bootstrap requires a standard service credential')
    origin, ca_file = _origin(value['origin']), _path(value['ca_file'])
    return BootstrapTransport(identity, origin, ca_file, node_id, token, _read_private(ca_file, MAX_CA_BYTES))
